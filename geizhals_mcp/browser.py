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

# Concurrency is deliberately tiny: each context is a real browser profile, and
# hammering Cloudflare in parallel from one datacenter IP is the fastest way to
# get the whole IP challenged or blocked. A chat agent issues one request at a
# time anyway.
MAX_CONCURRENT = int(os.getenv("GH_MAX_CONCURRENT", "2"))

# How long to wait for the Cloudflare interstitial to hand off to the real page.
CHALLENGE_TIMEOUT_MS = int(os.getenv("GH_CHALLENGE_TIMEOUT_MS", "25000"))

# Fresh-context retries when the challenge stalls (probabilistic from a server IP).
MAX_ATTEMPTS = int(os.getenv("GH_MAX_ATTEMPTS", "3"))

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


class BrowserManager:
    """Owns one Chromium instance and vends short-lived contexts."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._semaphore = asyncio.Semaphore(max_concurrent)

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
        """Load `url`, wait past the Cloudflare interstitial, return page HTML.

        Cloudflare's managed challenge is probabilistic from a server IP: the
        same URL may sail through on one attempt and stall on the next. So each
        fetch gets a couple of tries, each in a *fresh* context (a new browser
        fingerprint), before giving up.

        Raises `CloudflareBlocked` if every attempt stalls on the interstitial —
        the caller turns that into a clean tool error rather than a stack trace,
        since it is the single most likely failure mode here.
        """
        last_error: CloudflareBlocked | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await self._fetch_once(url)
            except CloudflareBlocked as exc:
                last_error = exc
                log.warning("Cloudflare stalled on attempt %s/%s for %s", attempt, MAX_ATTEMPTS, url)
        raise last_error or CloudflareBlocked("Cloudflare challenge did not clear")

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
        raise CloudflareBlocked(
            "Cloudflare challenge did not clear in time — the datacenter IP is "
            "likely flagged. A residential/mobile egress proxy is usually the fix."
        )


class CloudflareBlocked(RuntimeError):
    """Raised when the Cloudflare interstitial never hands off to the real page."""
