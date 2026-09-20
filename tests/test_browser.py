import asyncio
import time
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

from geizhals_mcp import browser


class BrowserHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_challenge_retries_are_bounded_and_start_cooldown(self) -> None:
        manager = browser.BrowserManager(max_concurrent=1)
        manager._fetch_once = AsyncMock(
            side_effect=browser.CloudflareBlocked("challenge")
        )

        with (
            patch.object(browser, "MAX_ATTEMPTS", 2),
            patch.object(browser, "MIN_REQUEST_INTERVAL_SECONDS", 0),
            patch.object(browser, "CLOUDFLARE_COOLDOWN_SECONDS", 0.05),
        ):
            with self.assertRaisesRegex(browser.CloudflareBlocked, "requests are paused"):
                await manager.fetch_html("https://geizhals.de/search")
            self.assertEqual(manager._fetch_once.await_count, 2)

            with self.assertRaisesRegex(browser.CloudflareBlocked, "cooldown is active"):
                await manager.fetch_html("https://geizhals.de/another-search")
            self.assertEqual(manager._fetch_once.await_count, 2)

            await asyncio.sleep(0.06)
            with self.assertRaises(browser.CloudflareBlocked):
                await manager.fetch_html("https://geizhals.de/third-search")
            self.assertEqual(manager._fetch_once.await_count, 4)

    async def test_navigation_starts_are_paced_across_retries(self) -> None:
        manager = browser.BrowserManager(max_concurrent=1)
        manager._fetch_once = AsyncMock(
            side_effect=[browser.CloudflareBlocked("challenge"), "<html />"]
        )

        with (
            patch.object(browser, "MAX_ATTEMPTS", 2),
            patch.object(browser, "MIN_REQUEST_INTERVAL_SECONDS", 0.03),
            patch.object(browser, "CLOUDFLARE_COOLDOWN_SECONDS", 0),
        ):
            started = time.monotonic()
            self.assertEqual(await manager.fetch_html("https://geizhals.de/search"), "<html />")
            self.assertGreaterEqual(time.monotonic() - started, 0.025)
            self.assertEqual(manager._fetch_once.await_count, 2)

    async def test_retries_back_off_exponentially_up_to_the_cap(self) -> None:
        manager = browser.BrowserManager(max_concurrent=1)
        manager._fetch_once = AsyncMock(
            side_effect=browser.CloudflareBlocked("challenge")
        )

        with (
            patch.object(browser, "MAX_ATTEMPTS", 3),
            patch.object(browser, "MIN_REQUEST_INTERVAL_SECONDS", 0.02),
            patch.object(browser, "CLOUDFLARE_COOLDOWN_SECONDS", 0),
            patch.object(browser, "BACKOFF_CAP_SECONDS", 0.03),
        ):
            started = time.monotonic()
            with self.assertRaises(browser.CloudflareBlocked):
                await manager.fetch_html("https://geizhals.de/search")
            # Backoff after failure 1 is one interval, after failure 2 two
            # intervals capped at BACKOFF_CAP_SECONDS: 0.02 + 0.03.
            self.assertGreaterEqual(time.monotonic() - started, 0.05)
            self.assertEqual(manager._fetch_once.await_count, 3)

    async def test_rate_limit_exhaustion_does_not_blame_cloudflare(self) -> None:
        manager = browser.BrowserManager(max_concurrent=1)
        manager._fetch_once = AsyncMock(side_effect=browser.RateLimited("429"))

        with (
            patch.object(browser, "MAX_ATTEMPTS", 2),
            patch.object(browser, "MIN_REQUEST_INTERVAL_SECONDS", 0),
            patch.object(browser, "CLOUDFLARE_COOLDOWN_SECONDS", 0),
            self.assertRaisesRegex(browser.CloudflareBlocked, "rate-limited \\(429\\)"),
        ):
            await manager.fetch_html("https://geizhals.de/search")
        self.assertEqual(manager._fetch_once.await_count, 2)

    async def test_rate_limit_retry_honors_retry_after(self) -> None:
        manager = browser.BrowserManager(max_concurrent=1)
        manager._fetch_once = AsyncMock(
            side_effect=[browser.RateLimited("429", retry_after=0.05), "<html />"]
        )

        with (
            patch.object(browser, "MAX_ATTEMPTS", 2),
            patch.object(browser, "MIN_REQUEST_INTERVAL_SECONDS", 0),
            patch.object(browser, "CLOUDFLARE_COOLDOWN_SECONDS", 0),
        ):
            started = time.monotonic()
            self.assertEqual(await manager.fetch_html("https://geizhals.de/search"), "<html />")
            # The 429's Retry-After (0.05s) is slept even though the backoff
            # base (MIN_REQUEST_INTERVAL_SECONDS) is zero.
            self.assertGreaterEqual(time.monotonic() - started, 0.045)
            self.assertEqual(manager._fetch_once.await_count, 2)

    async def test_retry_after_header_parsing(self) -> None:
        class StubResponse:
            def __init__(self, header: str | None) -> None:
                self._header = header

            async def header_value(self, name: str) -> str | None:
                return self._header

        self.assertEqual(await browser._retry_after_seconds(StubResponse("7")), 7.0)
        self.assertEqual(await browser._retry_after_seconds(StubResponse(None)), 0.0)
        self.assertEqual(await browser._retry_after_seconds(StubResponse("soon")), 0.0)
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30))
        seconds = await browser._retry_after_seconds(StubResponse(future))
        self.assertGreater(seconds, 25.0)
        self.assertLessEqual(seconds, 30.0)


if __name__ == "__main__":
    unittest.main()
