"""Parse Geizhals search and product HTML into plain dicts.

Geizhals has no public API, so this parses rendered search HTML and directly
fetched product-page HTML:

* **Search** results render as ``galleryview__*`` tiles — there is no JSON-LD on
  the SERP — so each product is assembled from the three per-tile anchors
  (``name-link`` / ``price-link`` / ``offercount-link``) that all share the same
  ``…-a<id>.html`` href.
* **Product** pages have a server-rendered offer table. Its rows contain the
  merchant, price, click-out URL and (where shown) availability.

Product URLs are ``<slug>-a<id>.html``; the bare ``a<id>.html`` form redirects
to the canonical slug, which is what `get_product` relies on.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

BASE_URL = "https://geizhals.de/"


class ParseError(RuntimeError):
    """Raised when a page lacks the structure we parse — drift or error page."""


# Product id from a Geizhals URL: canonical is "<slug>-a<id>.html"; the bare
# "/a<id>.html" redirect form is matched too.
_PRODUCT_HREF = re.compile(r"[/-]a(\d+)\.html")
# German price: "€ 1.299,00" / "749,00" -> 1299.00 / 749.00
_PRICE = re.compile(r"(\d[\d.\s]*),(\d{2})")

# The few HTML selectors still used, kept in one place. Product details locate
# their server-rendered offer table structurally because its class names vary.
_SEL: dict[str, str] = {
    "name_link": "a.galleryview__name-link",
    "price_link": "a.galleryview__price-link",
    "offercount_link": "a.galleryview__offercount-link",
}


def search_url(query: str, *, sort: str | None = None, region: str = "de") -> str:
    """Build a Geizhals free-text search URL."""
    url = f"{BASE_URL}?fs={quote_plus(query)}&hloc={region}"
    if sort:
        # p = cheapest first, r = relevance/rank. Anything else Geizhals ignores.
        url += f"&sort={sort}"
    return url


def _to_eur(text: str | None) -> float | None:
    if not text:
        return None
    m = _PRICE.search(text)
    if not m:
        return None
    whole = m.group(1).replace(".", "").replace(" ", "")
    return float(f"{whole}.{m.group(2)}")


def _int_of(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def _url_id(url: str | None) -> str | None:
    if not url:
        return None
    m = _PRODUCT_HREF.search(url)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #

# Import here so a bs4-less environment still imports the pure helpers above.
from bs4 import BeautifulSoup  # noqa: E402

# Text snippets that positively identify an empty result set, in German and
# English. Only consulted when zero tiles matched, so a results page carrying
# one of these strings inside unrelated copy can never trigger them.
_NO_RESULTS_MARKERS = (
    "keine treffer",
    "keine ergebnisse",
    "keine produkte gefunden",
    "nichts gefunden",
    "no results",
    "no products found",
)


def parse_search(html: str, *, max_results: int) -> list[dict[str, Any]]:
    """Extract product summaries from a search results page.

    Each Geizhals tile exposes its product through several anchors that share
    one ``…-a<id>.html`` href; we group them by that id so name, price and offer
    count land on one record. Order follows the page's own ranking.

    Raises `ParseError` when the page shows neither product tiles nor a
    recognizable empty-results state — selector drift or an error page must
    not masquerade as a successful search with zero hits.
    """
    soup = BeautifulSoup(html, "lxml")
    products: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    selector = ", ".join(
        (_SEL["name_link"], _SEL["price_link"], _SEL["offercount_link"])
    )
    for a in soup.select(selector):
        pid = _url_id(a.get("href", ""))
        if not pid:
            continue
        rec = products.get(pid)
        if rec is None:
            rec = {
                "id": pid,
                "name": None,
                "price": None,
                "currency": "EUR",
                "offer_count": None,
                "url": f"{BASE_URL}a{pid}.html",
            }
            products[pid] = rec
            order.append(pid)

        classes = a.get("class") or []
        text = a.get_text(" ", strip=True)
        if "galleryview__name-link" in classes:
            rec["name"] = text or rec["name"]
            rec["url"] = (a.get("href") or rec["url"]).split("#")[0]
        elif "galleryview__price-link" in classes:
            rec["price"] = _to_eur(text)
        elif "galleryview__offercount-link" in classes:
            rec["offer_count"] = _int_of(text)

    if products:
        hits = [products[i] for i in order if products[i]["name"]]
        if not hits:
            raise ParseError(
                "search page has product tile anchors but no name links; "
                "selectors may have drifted or an error page was served"
            )
        return hits[:max_results]

    text = soup.get_text(" ", strip=True).lower()
    if any(marker in text for marker in _NO_RESULTS_MARKERS):
        return []
    # The SERP always wraps its listing in #results (the page's own
    # "skip to results" anchor points there): a shell without tiles is a
    # positively recognized empty result set.
    if soup.select_one("#results") is not None:
        return []
    raise ParseError(
        "search page has no product tiles and no recognizable no-results "
        "state; selectors may have drifted or an error page was served"
    )


# --------------------------------------------------------------------------- #
# product detail
# --------------------------------------------------------------------------- #


def parse_product(html: str, product_id: str) -> dict[str, Any]:
    """Extract a product's name, price range and per-merchant offer table."""
    soup = BeautifulSoup(html, "lxml")
    name = _text(soup.select_one("h1"))
    try:
        table = _offer_table(soup)
    except ParseError as exc:
        raise ParseError(f"product page for {product_id} {exc}") from exc
    offers: list[dict[str, Any]] = []

    for row in table.find_all("tr"):
        merchant = _field(row, "merchant", "shop", "seller", "dealer")
        price_field = _field(row, "price", "preis")
        price = _to_eur(_text(price_field))
        if not merchant or price is None:
            continue
        link = (price_field and price_field.find("a", href=True)) or merchant.find(
            "a", href=True
        )
        offers.append(
            {
                "merchant": _text(merchant),
                "price": price,
                "currency": "EUR",
                "url": link.get("href") if link else None,
                "availability": _text(
                    _field(row, "availability", "delivery", "stock", "liefer")
                ),
            }
        )

    if not name or not offers:
        raise ParseError(
            f"product page for {product_id} has no recognizable name and offer "
            "table; selectors may have drifted or an error page was served"
        )

    canonical = soup.select_one('link[rel~="canonical"]')
    url = canonical.get("href") if canonical else None
    if url and _url_id(url) not in (None, product_id):
        raise ParseError(
            f"product page canonical URL does not match requested id {product_id}"
        )
    offers.sort(key=lambda offer: offer["price"])
    prices = [offer["price"] for offer in offers]
    return {
        "id": product_id,
        "name": name,
        "url": url or f"{BASE_URL}a{product_id}.html",
        "price_min": min(prices),
        "price_max": max(prices),
        "currency": "EUR",
        "offer_count": len(offers),
        "offers": offers,
    }


def _offer_table(soup: "BeautifulSoup") -> Any:
    """Return the product's explicitly labelled server-rendered offer table."""
    for table in soup.find_all("table"):
        identifier = " ".join(
            str(value) for value in (*table.get("class", ()), table.get("id", ""))
        ).lower()
        if "offer" in identifier or "angebot" in identifier:
            return table
    raise ParseError(
        "product page has no recognizable offer table; selectors may have "
        "drifted or an error page was served"
    )


def _field(row: Any, *names: str) -> Any:
    """First descendant whose class identifies one of the requested fields."""
    for tag in row.find_all(True):
        classes = " ".join(tag.get("class") or ()).lower()
        if any(name in classes for name in names):
            return tag
    return None


def _text(tag: Any) -> str | None:
    return tag.get_text(" ", strip=True) or None if tag else None
