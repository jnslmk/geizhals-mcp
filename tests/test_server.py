import unittest
from unittest.mock import AsyncMock, Mock, patch

from geizhals_mcp import server


def _product_html(pid: str) -> str:
    return f"""
<html><head><link rel="canonical" href="https://geizhals.de/foo-a{pid}.html"></head>
<body><h1>Foo</h1><table id="offerlist"><tr>
<td class="offerlist__shop"><a href="https://merchant.example/">M</a></td>
<td class="offerlist__price">&euro; 5,00</td>
</tr></table></body></html>
"""


class ValidateScrapeUrlTests(unittest.TestCase):
    def test_geizhals_hosts_and_www_forms_are_allowed(self) -> None:
        for url in (
            "https://geizhals.de/?fs=rtx+4070",
            "https://geizhals.at/cat/123",
            "https://geizhals.eu/?fs=x",
            "https://www.geizhals.de/?fs=x",
            "https://www.geizhals.eu/cat/9",
            # hostname matching is case-insensitive
            "https://GEIZHALS.DE/?fs=x",
        ):
            with self.subTest(url=url):
                server._validate_scrape_url(url)  # must not raise

    def test_everything_else_is_rejected(self) -> None:
        for url in (
            # the original bug: a foreign URL merely mentioning geizhals
            "https://evil.example/?x=geizhals.de",
            "https://geizhals.de.evil.example/",
            "http://geizhals.de/",  # not https
            "https://geizhals.de:8443/",  # explicit port
            "https://user@geizhals.de/",  # credentials
            "https://user:pass@geizhals.de/",
            "https://192.168.0.1/?x=geizhals.de",
            "ftp://geizhals.de/",
            "not a url at all",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    server._validate_scrape_url(url)


class SearchProductsTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_one_browser_search_fetch(self) -> None:
        url = server.scraper.search_url("RTX 4070", sort="r")
        fetch = AsyncMock(return_value="<html />")
        with (
            patch.object(server, "_fetch_search", fetch),
            patch.object(server.scraper, "parse_search", return_value=[]),
        ):
            result = await server.search_products("RTX 4070")

        fetch.assert_awaited_once_with(url)
        self.assertEqual(result, {"query": "RTX 4070", "returned": 0, "results": []})


class GetProductTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_direct_product_transport(self) -> None:
        fetch = AsyncMock(return_value=_product_html("42"))
        with patch.object(server, "_fetch_product", fetch):
            product = await server.get_product(" 42 ")

        fetch.assert_awaited_once_with("https://geizhals.de/a42.html")
        self.assertEqual(product["id"], "42")


class GetProductsBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_ids_failing_must_not_report_success(self) -> None:
        with patch.object(
            server, "_fetch_product", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            result = await server.get_products_batch(product_ids=["42", "43"])
        self.assertFalse(result["success"])
        self.assertEqual(result["returned"], 0)
        self.assertEqual([e["id"] for e in result["errors"]], ["42", "43"])

    async def test_partial_failure_reports_success_false_with_results(self) -> None:
        with patch.object(
            server,
            "_fetch_product",
            AsyncMock(side_effect=[RuntimeError("boom"), _product_html("43")]),
        ):
            result = await server.get_products_batch(product_ids=["42", "43"])
        self.assertFalse(result["success"])
        self.assertEqual(result["returned"], 1)
        self.assertEqual(result["results"][0]["id"], "43")
        self.assertEqual([e["id"] for e in result["errors"]], ["42"])

    async def test_full_success_still_reports_success_true(self) -> None:
        with patch.object(
            server,
            "_fetch_product",
            AsyncMock(side_effect=[_product_html("42"), _product_html("43")]),
        ):
            result = await server.get_products_batch(product_ids=["42", "43"])
        self.assertTrue(result["success"])
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["errors"], [])





class GetPriceHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_wraps_untouched_upstream_history_without_browser(self) -> None:
        upstream_response = [[1720000000, 99.95]]
        upstream_meta = {"currency": "EUR"}
        fetch = AsyncMock(return_value=(upstream_response, upstream_meta))
        with (
            patch.object(server, "_fetch_price_history", fetch),
            patch.object(
                server, "_search_manager", side_effect=AssertionError("browser must not be used")
            ),
        ):
            result = await server.get_price_history(" 42 ", days="91", loc="at")

        fetch.assert_awaited_once_with(42, 91, "at")
        self.assertEqual(
            result,
            {
                "product_id": 42,
                "days": 91,
                "loc": "at",
                "response": upstream_response,
                "meta": upstream_meta,
            },
        )
        self.assertIs(result["response"], upstream_response)
        self.assertIs(result["meta"], upstream_meta)

    async def test_rejects_invalid_inputs_before_request(self) -> None:
        fetch = AsyncMock()
        invalid_calls = (
            {"product_id": "not-an-id"},
            {"product_id": 42, "days": 8},
            {"product_id": 42, "days": "seven"},
            {"product_id": 42, "loc": "eu"},
        )
        with patch.object(server, "_fetch_price_history", fetch):
            for kwargs in invalid_calls:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    await server.get_price_history(**kwargs)
        fetch.assert_not_awaited()


class FetchPriceHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_exact_single_id_payload(self) -> None:
        response = Mock()
        response.json.return_value = {"response": [[1720000000, 99.95]], "meta": {"foo": "bar"}}
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client

        with patch.object(server.httpx, "AsyncClient", return_value=client) as factory:
            result = await server._fetch_price_history(42, 31, "de")

        self.assertEqual(result, ([[1720000000, 99.95]], {"foo": "bar"}))
        factory.assert_called_once_with(follow_redirects=True)
        client.post.assert_awaited_once_with(
            "https://geizhals.de/api/gh0/price_history",
            json={"id": [42], "params": {"days": 31, "loc": "de"}},
        )
        response.raise_for_status.assert_called_once_with()

    async def test_rejects_malformed_upstream_body(self) -> None:
        response = Mock()
        response.json.return_value = {"response": []}
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client

        with (
            patch.object(server.httpx, "AsyncClient", return_value=client),
            self.assertRaisesRegex(RuntimeError, "must contain response and meta"),
        ):
            await server._fetch_price_history(42, 31, "de")

class FetchProductTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_redirects_with_plain_httpx(self) -> None:
        response = Mock(text="<html>product</html>")
        client = AsyncMock()
        client.get.return_value = response
        client.__aenter__.return_value = client

        with patch.object(server.httpx, "AsyncClient", return_value=client) as factory:
            html = await server._fetch_product("https://geizhals.de/a42.html")

        self.assertEqual(html, "<html>product</html>")
        factory.assert_called_once_with(follow_redirects=True)
        client.get.assert_awaited_once_with("https://geizhals.de/a42.html")
        response.raise_for_status.assert_called_once_with()

if __name__ == "__main__":
    unittest.main()
