import unittest

from geizhals_mcp import scraper

_TILE_HTML = """
<html><body><div id="results">
  <a class="galleryview__name-link" href="https://geizhals.de/foo-a123456.html">Foo Bar</a>
  <a class="galleryview__price-link" href="https://geizhals.de/foo-a123456.html">&euro; 123,45</a>
  <a class="galleryview__offercount-link" href="https://geizhals.de/foo-a123456.html">12 Angebote</a>
</div></body></html>
"""

_PRODUCT_TABLE_HTML = """
<html><head>
  <link rel="canonical" href="https://geizhals.de/foo8-a123456.html">
</head><body>
  <h1>Foo 8GB</h1>
  <table id="offerlist">
    <tr>
      <td class="offerlist__shop"><a href="https://merchant2.example/y">Merchant 2</a></td>
      <td class="offerlist__price">&euro; 120,00</td>
      <td class="offerlist__delivery">2–4 Tage</td>
    </tr>
    <tr>
      <td class="offerlist__shop"><a href="https://merchant1.example/x">Merchant 1</a></td>
      <td class="offerlist__price">&euro; 99,00</td>
      <td class="offerlist__delivery">lagernd</td>
    </tr>
  </table>
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

    def test_tiles_without_name_links_raise_instead_of_returning_empty(self) -> None:
        # Tile anchors matched, but none carries a name link: a drifted tile
        # shape must not masquerade as a successful search with zero hits.
        html = """
        <html><body><div id="results">
          <a class="galleryview__price-link" href="https://geizhals.de/foo-a123456.html">&euro; 123,45</a>
          <a class="galleryview__offercount-link" href="https://geizhals.de/foo-a123456.html">12 Angebote</a>
        </div></body></html>
        """
        with self.assertRaisesRegex(scraper.ParseError, "no name links"):
            scraper.parse_search(html, max_results=10)


class ParseProductTests(unittest.TestCase):
    def test_offer_table_is_parsed_with_sorted_offers(self) -> None:
        product = scraper.parse_product(_PRODUCT_TABLE_HTML, "123456")
        self.assertEqual(product["name"], "Foo 8GB")
        self.assertEqual(product["url"], "https://geizhals.de/foo8-a123456.html")
        self.assertEqual(product["price_min"], 99.0)
        self.assertEqual(product["price_max"], 120.0)
        self.assertEqual(product["currency"], "EUR")
        self.assertEqual(product["offer_count"], 2)
        self.assertEqual(
            [offer["merchant"] for offer in product["offers"]],
            ["Merchant 1", "Merchant 2"],
        )
        self.assertEqual(product["offers"][0]["availability"], "lagernd")

    def test_unrecognized_product_markup_raises(self) -> None:
        html = "<html><body><h1>Foo Bar</h1><p>Keine Angebote.</p></body></html>"
        with self.assertRaisesRegex(scraper.ParseError, "123456"):
            scraper.parse_product(html, "123456")


if __name__ == "__main__":
    unittest.main()
