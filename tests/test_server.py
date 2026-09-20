import unittest
from unittest.mock import AsyncMock, patch

from geizhals_mcp import server

def _product_html(pid: str) -> str:
    return f"""
<html><body><script type="application/ld+json">
{{"@context": "https://schema.org", "@type": "Product", "name": "Foo",
 "url": "https://geizhals.de/foo-a{pid}.html",
 "offers": {{"@type": "AggregateOffer", "lowPrice": "5.00", "offerCount": 1,
            "offers": [{{"@type": "Offer", "price": "5.00",
                        "seller": {{"@type": "Organization", "name": "M"}}}}]}}}}
</script></body></html>
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


class GetProductsBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_ids_failing_must_not_report_success(self) -> None:
        with patch.object(
            server, "_fetch", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            result = await server.get_products_batch(product_ids=["42", "43"])
        self.assertFalse(result["success"])
        self.assertEqual(result["returned"], 0)
        self.assertEqual([e["id"] for e in result["errors"]], ["42", "43"])

    async def test_partial_failure_reports_success_false_with_results(self) -> None:
        with patch.object(
            server,
            "_fetch",
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
            "_fetch",
            AsyncMock(side_effect=[_product_html("42"), _product_html("43")]),
        ):
            result = await server.get_products_batch(product_ids=["42", "43"])
        self.assertTrue(result["success"])
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["errors"], [])


if __name__ == "__main__":
    unittest.main()
