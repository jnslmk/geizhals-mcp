"""Headless-browser lifecycle for scraping Geizhals.

Geizhals sits behind a Cloudflare JS challenge ("Sichere Verbindung wird
überprüft"), so a plain HTTP client gets a 403 and this has to drive a real
browser that can execute the challenge script. Vanilla Playwright is trivially
fingerprinted by Cloudflare, so we use **patchright** — a drop-in patched
Playwright fork built to defeat that detection — and, in the container, run the
browser *headed* under Xvfb (headless Chromium is far easier for Cloudflare to
flag than a headed one behind a virtual display).

Unlike kleinanzeigen-mcp, there is no upstream scraper library to lean on, so
this module owns the whole browser lifecycle itself: one shared browser, one
fresh context per request (guarded by a semaphore), and a small helper that
waits for the Cloudflare interstitial to clear before returning the page HTML.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# patchright mirrors playwright's async API surface exactly, so this import is a
# straight swap for `from playwright.async_api import ...`.
from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

log = logging.getLogger("geizhals-mcp.browser")

# Keep direct scraping deliberately gentle. The upper bound prevents an
# environment mistake from turning one MCP call into a request burst.
MAX_CONCURRENT = max(1, min(int(os.getenv("GH_MAX_CONCURRENT", "1")), 2))

# Minimum delay between browser navigations, shared by all tool calls.
MIN_REQUEST_INTERVAL_SECONDS = max(
    0.0, float(os.getenv("GH_MIN_REQUEST_INTERVAL_SECONDS", "2"))
)

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


class BrowserManager:
    """Owns one Chromium instance and vends short-lived contexts."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
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
        log.info(
            "Chromium ready (headless=%s, max_concurrent=%s, proxy=%s)",
            HEADLESS,
            MAX_CONCURRENT,
            PROXY_SERVER or "none",
        )

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @property
    def ready(self) -> bool:
        return self._browser is not None

    @asynccontextmanager
    async def context(self) -> AsyncIterator[BrowserContext]:
        """A fresh, German-locale browser context, one at a time per semaphore slot."""
        if self._browser is None:
            raise RuntimeError("Browser manager is not running")
        async with self._semaphore:
            ctx = await self._browser.new_context(
                locale="de-DE",
                timezone_id="Europe/Berlin",
                viewport={"width": 1366, "height": 900},
                # Consent cookie so Geizhals skips the CMP wall and serves the
                # listing directly. Harmless if the name drifts — the page still
                # loads, just with the banner.
                extra_http_headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.6"},
            )
            try:
                yield ctx
            finally:
                await ctx.close()

    async def fetch_html(self, url: str) -> str:
        """Load `url`, returning bounded, explicit Cloudflare failures.

        Navigations are globally paced even when several MCP calls arrive at
        once. Challenge failures get at most ``MAX_ATTEMPTS`` fresh contexts;
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
                    "Cloudflare stalled on attempt %s/%s for %s",
                    attempt,
                    MAX_ATTEMPTS,
                    url,
                )
        retry_after = await self._start_cooldown()
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
            self._next_request_at = now + MIN_REQUEST_INTERVAL_SECONDS

    async def _start_cooldown(self) -> float:
        async with self._request_gate:
            self._cooldown_until = max(
                self._cooldown_until,
                time.monotonic() + CLOUDFLARE_COOLDOWN_SECONDS,
            )
            return max(0.0, self._cooldown_until - time.monotonic())

    async def _fetch_once(self, url: str) -> str:
        async with self.context() as ctx:
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await self._await_challenge(page)
            # The product-page JSON-LD (and the offer table) is hydrated by JS
            # after domcontentloaded, so wait for the network to go idle before
            # snapshotting. Best-effort: a busy page that never idles still
            # returns whatever has rendered so far rather than erroring.
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            await page.wait_for_timeout(1000)
            return await page.content()

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

