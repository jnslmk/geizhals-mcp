import asyncio
import time
import unittest
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


if __name__ == "__main__":
    unittest.main()
