import unittest

from geizhals_mcp import scraper

_TILE_HTML = """
<html><body><div id="results">
  <a class="galleryview__name-link" href="https://geizhals.de/foo-a123456.html">Foo Bar</a>
  <a class="galleryview__price-link" href="https://geizhals.de/foo-a123456.html">&euro; 123,45</a>
  <a class="galleryview__offercount-link" href="https://geizhals.de/foo-a123456.html">12 Angebote</a>
</div></body></html>
"""

_PRODUCT_LD_HTML = """
<html><body>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "ProductGroup",
 "hasVariant": [{
   "@type": "Product", "name": "Foo 8GB",
   "url": "https://geizhals.de/foo8-a123456.html",
   "offers": {"@type": "AggregateOffer", "lowPrice": "99.00",
              "highPrice": "149.50", "priceCurrency": "EUR", "offerCount": 3,
              "offers": [
                {"@type": "Offer", "price": "120.00", "priceCurrency": "EUR",
                 "url": "https://merchant2.example/y",
                 "seller": {"@type": "Organization", "name": "Merchant 2"}},
                {"@type": "Offer", "price": "99.00", "priceCurrency": "EUR",
                 "url": "https://merchant1.example/x",
                 "seller": {"@type": "Organization", "name": "Merchant 1"},
                 "availability": "https://schema.org/InStock"}]}}]}
</script>
</body></html>
"""


class ParseSearchTests(unittest.TestCase):
    def test_tiles_are_grouped_into_summaries(self) -> None:
        results = scraper.parse_search(_TILE_HTML, max_results=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0],
            {
                "id": "123456",
                "name": "Foo Bar",
                "price": 123.45,
                "currency": "EUR",
                "offer_count": 12,
                "url": "https://geizhals.de/foo-a123456.html",
            },
        )

    def test_results_shell_without_tiles_is_a_recognized_empty_page(self) -> None:
        html = '<html><body><div id="results"></div></body></html>'
        self.assertEqual(scraper.parse_search(html, max_results=10), [])

    def test_no_results_marker_is_a_recognized_empty_page(self) -> None:
        html = "<html><body><p>Leider keine Treffer.</p></body></html>"
        self.assertEqual(scraper.parse_search(html, max_results=10), [])

    def test_unrecognizable_page_raises_instead_of_returning_empty(self) -> None:
        with self.assertRaisesRegex(scraper.ParseError, "drifted"):
            scraper.parse_search("<html><body>Just a moment...</body></html>", max_results=10)


class ParseProductTests(unittest.TestCase):
    def test_json_ld_product_group_is_parsed_with_sorted_offers(self) -> None:
        product = scraper.parse_product(_PRODUCT_LD_HTML, "123456")
        self.assertEqual(product["name"], "Foo 8GB")
        self.assertEqual(product["price_min"], 99.0)
        self.assertEqual(product["price_max"], 149.5)
        self.assertEqual(product["currency"], "EUR")
        self.assertEqual([o["merchant"] for o in product["offers"]],
                         ["Merchant 1", "Merchant 2"])
        self.assertEqual(product["offers"][0]["availability"], "InStock")

    def test_missing_json_ld_raises_instead_of_zero_offers(self) -> None:
        html = "<html><body><h1>Foo Bar</h1><p>Keine Angebote.</p></body></html>"
        with self.assertRaisesRegex(scraper.ParseError, "123456"):
            scraper.parse_product(html, "123456")

    def test_malformed_json_ld_raises_instead_of_zero_offers(self) -> None:
        html = ('<html><body><script type="application/ld+json">{broken'
                "</script></body></html>")
        with self.assertRaises(scraper.ParseError):
            scraper.parse_product(html, "123456")


if __name__ == "__main__":
    unittest.main()
