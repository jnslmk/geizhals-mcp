"""Browser lifecycle for browser-gated Geizhals search pages.

Geizhals search pages sit behind a Cloudflare JS challenge ("Sichere Verbindung
wird überprüft"), so a plain HTTP client gets a 403 and this has to drive a
real browser that can execute the challenge script. Vanilla Playwright is
trivially fingerprinted by Cloudflare, so we use **patchright** — a drop-in
patched Playwright fork built to defeat that detection — and, in the container,
run the browser *headed* under Xvfb (headless Chromium is far easier for
Cloudflare to flag than a headed one behind a virtual display).

This module owns browser state for search surfaces only: one shared browser
with one long-lived context per process (cookies and any Cloudflare clearance
survive across navigations), a semaphore plus global pacing around each search
navigation, and a helper that waits for the Cloudflare interstitial before
returning search HTML.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# patchright mirrors playwright's async API surface exactly, so this import is a
# straight swap for `from playwright.async_api import ...`.
from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Response,
    async_playwright,
)

log = logging.getLogger("geizhals-mcp.browser")

# Keep browser-gated search scraping deliberately gentle. The upper bound
# prevents an environment mistake from turning one MCP search call into a
# request burst.
MAX_CONCURRENT = max(1, min(int(os.getenv("GH_MAX_CONCURRENT", "1")), 2))

# Minimum delay between browser search navigations, shared by search tools.
MIN_REQUEST_INTERVAL_SECONDS = max(
    0.0, float(os.getenv("GH_MIN_REQUEST_INTERVAL_SECONDS", "2"))
)

# Navigations are jittered around the minimum interval so repeated requests do
# not march in lockstep; a quarter of the interval keeps pacing predictable.
JITTER_FRACTION = 0.25

# Upper bound for the exponential backoff between retry attempts, so a large
# MIN_REQUEST_INTERVAL_SECONDS or Retry-After cannot stall a call for minutes.
BACKOFF_CAP_SECONDS = max(0.0, float(os.getenv("GH_BACKOFF_CAP_SECONDS", "30")))

# Once all attempts hit the challenge, fail subsequent calls fast until this
# window expires instead of repeatedly spending challenge timeouts.
CLOUDFLARE_COOLDOWN_SECONDS = max(
    0.0, float(os.getenv("GH_CLOUDFLARE_COOLDOWN_SECONDS", "60"))
)

# Keep retries finite and conservative. The cap is intentional even when an
# operator supplies an unexpectedly large environment value.
MAX_ATTEMPTS = min(max(int(os.getenv("GH_MAX_ATTEMPTS", "2")), 1), 3)

# How long to wait for the Cloudflare interstitial to hand off to the real page.
CHALLENGE_TIMEOUT_MS = int(os.getenv("GH_CHALLENGE_TIMEOUT_MS", "25000"))

# Headed-under-Xvfb by default (best against Cloudflare). Set GH_HEADLESS=1 for
# local development on a machine without a display server.
HEADLESS = os.getenv("GH_HEADLESS", "0") == "1"

# A current stable desktop Chrome UA. The Chromium patchright launches reports
# its real (automation-flagged) UA otherwise, and a stale or nonstandard UA is
# an easy Cloudflare/Geizhals signal. GH_USER_AGENT overrides the default when
# it ages out.
CHROME_USER_AGENT = os.getenv("GH_USER_AGENT") or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Headers sent alongside the UA on every context request, matching what the
# browser above would send for a document navigation.
CHROME_EXTRA_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
}

# Optional egress proxy. Cloudflare hard-blocks datacenter IPs, so a deployment
# on a server routes the browser through a residential IP (e.g. an HTTP proxy on
# a home box reachable over the tailnet). GH_PROXY is a full proxy URL such as
# "http://100.112.187.107:8888"; auth is optional via GH_PROXY_USERNAME/PASSWORD.
PROXY_SERVER = os.getenv("GH_PROXY") or None
PROXY_USERNAME = os.getenv("GH_PROXY_USERNAME") or None
PROXY_PASSWORD = os.getenv("GH_PROXY_PASSWORD") or None


def _proxy_config() -> dict[str, str] | None:
    if not PROXY_SERVER:
        return None
    proxy: dict[str, str] = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        proxy["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        proxy["password"] = PROXY_PASSWORD
    return proxy


def _jittered_interval() -> float:
    """Minimum request interval with uniform jitter, so calls do not sync up."""
    return MIN_REQUEST_INTERVAL_SECONDS * random.uniform(
        1.0 - JITTER_FRACTION, 1.0 + JITTER_FRACTION
    )


def _backoff_seconds(failed_attempt: int) -> float:
    """Bounded exponential backoff after retry attempt `failed_attempt`."""
    return min(
        MIN_REQUEST_INTERVAL_SECONDS * 2 ** (failed_attempt - 1),
        BACKOFF_CAP_SECONDS,
    )


async def _retry_after_seconds(response: Response) -> float:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date); 0 if absent."""
    value = await response.header_value("retry-after")
    if not value:
        return 0.0
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


# Markers that mean "still on the Cloudflare interstitial, not the real page".
_CHALLENGE_MARKERS = (
    "just a moment",
    "sichere verbindung wird",
    "checking your browser",
    "cf-challenge",
    "challenge-platform",
)


class CloudflareBlocked(RuntimeError):
    """Raised when Cloudflare blocks a request or the cooldown is active."""

    def __init__(self, message: str, *, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = max(0.0, retry_after)


class RateLimited(CloudflareBlocked):
    """Raised on an HTTP 429; shares the retry/backoff/cooldown machinery."""


class BrowserManager:
    """Owns browser state for browser-gated search surfaces."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._request_gate = asyncio.Lock()
        self._next_request_at = 0.0
        self._cooldown_until = 0.0

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        proxy = _proxy_config()
        self._browser = await self._playwright.chromium.launch(
            headless=HEADLESS,
            proxy=proxy,
            args=[
                # Required in a container: Chromium's own sandbox needs either
                # privileged caps or user namespaces we do not grant.
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        # One long-lived context: cookies — including any Cloudflare clearance
        # and Geizhals consent choice — survive across navigations instead of
        # being thrown away with a per-request context, which is what makes
        # every request look new and invites repeated challenges.
        self._context = await self._browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1366, "height": 900},
            user_agent=CHROME_USER_AGENT,
            extra_http_headers=CHROME_EXTRA_HEADERS,
        )
        log.info(
            "Chromium ready (headless=%s, max_concurrent=%s, proxy=%s)",
            HEADLESS,
            MAX_CONCURRENT,
            PROXY_SERVER or "none",
        )

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @property
    def ready(self) -> bool:
        return self._browser is not None

    async def fetch_search_html(self, url: str) -> str:
        """Load a search URL, returning bounded, explicit Cloudflare failures.

        Search navigations are globally paced even when several MCP calls arrive
        at once. Challenge and 429 failures get at most ``MAX_ATTEMPTS`` tries,
        with bounded exponential backoff (honoring Retry-After) in between;
        exhausting them starts a shared cooldown so follow-up calls fail fast.
        """
        last_error: CloudflareBlocked | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._wait_for_request_slot()
            try:
                return await self._fetch_once(url)
            except CloudflareBlocked as exc:
                last_error = exc
                log.warning(
                    "%s on attempt %s/%s for %s",
                    exc,
                    attempt,
                    MAX_ATTEMPTS,
                    url,
                )
                if attempt < MAX_ATTEMPTS:
                    delay = min(
                        max(_backoff_seconds(attempt), exc.retry_after),
                        BACKOFF_CAP_SECONDS,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
        retry_after = await self._start_cooldown()
        if isinstance(last_error, RateLimited):
            message = (
                f"Geizhals rate-limited (429) after {MAX_ATTEMPTS} attempts; "
                "requests are paused"
            )
        else:
            message = (
                f"Cloudflare challenge did not clear after {MAX_ATTEMPTS} "
                "attempts; requests are paused"
            )
        if retry_after:
            message += f" — retry in about {retry_after:.0f}s"
        raise CloudflareBlocked(message, retry_after=retry_after) from last_error

    async def _wait_for_request_slot(self) -> None:
        """Wait for pacing, or reject immediately during a shared cooldown."""
        async with self._request_gate:
            now = time.monotonic()
            if self._cooldown_until > now:
                retry_after = self._cooldown_until - now
                raise CloudflareBlocked(
                    "Cloudflare cooldown is active; retry in "
                    f"about {retry_after:.0f}s",
                    retry_after=retry_after,
                )
            wait = self._next_request_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_request_at = now + _jittered_interval()

    async def _start_cooldown(self) -> float:
        async with self._request_gate:
            self._cooldown_until = max(
                self._cooldown_until,
                time.monotonic() + CLOUDFLARE_COOLDOWN_SECONDS,
            )
            return max(0.0, self._cooldown_until - time.monotonic())

    async def _fetch_once(self, url: str) -> str:
        if self._context is None:
            raise RuntimeError("Browser manager is not running")
        async with self._semaphore:
            page = await self._context.new_page()
            try:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=45000
                )
                if response is not None and response.status == 429:
                    retry_after = await _retry_after_seconds(response)
                    if retry_after:
                        raise RateLimited(
                            "Geizhals rate-limited the request (429); "
                            f"retry after {retry_after:.0f}s",
                            retry_after=retry_after,
                        )
                    raise RateLimited(
                        "Geizhals rate-limited the request (429) without "
                        "a Retry-After header"
                    )
                await self._await_challenge(page)
                return await page.content()
            finally:
                await page.close()

    async def _await_challenge(self, page: Page) -> None:
        """Block until the page is no longer the Cloudflare interstitial."""
        deadline = CHALLENGE_TIMEOUT_MS
        step = 500
        waited = 0
        while waited < deadline:
            title = (await page.title() or "").lower()
            body = (await page.content())[:4000].lower()
            if not any(m in title or m in body for m in _CHALLENGE_MARKERS):
                return
            await page.wait_for_timeout(step)
            waited += step
        raise CloudflareBlocked("Cloudflare challenge did not clear in time")

