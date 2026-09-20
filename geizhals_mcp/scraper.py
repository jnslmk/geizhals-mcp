"""Parse Geizhals search and product HTML into plain dicts.

Geizhals has no public API, so this parses rendered HTML fetched by
`browser.py`. The selectors below were verified against live pages (July 2026):

* **Search** results render as ``galleryview__*`` tiles — there is no JSON-LD on
  the SERP — so each product is assembled from the three per-tile anchors
  (``name-link`` / ``price-link`` / ``offercount-link``) that all share the same
  ``…-a<id>.html`` href.
* **Product** pages carry a single ``application/ld+json`` block: a
  ``ProductGroup`` whose ``hasVariant[]`` entries are ``Product`` objects, each
  with an ``AggregateOffer`` that nests the individual per-merchant ``Offer``s
  (seller name, price, click-out url). That structured block is authoritative;
  the HTML offer table is only a fallback for the rare page without it.

Product URLs are ``<slug>-a<id>.html``; the bare ``a<id>.html`` form redirects
to the canonical slug, which is what `get_product` relies on.
"""

from __future__ import annotations

import json
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

# The few HTML selectors still used, kept in one place. The product-page parser
# is JSON-LD-first, so these only back the search SERP.
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
        return [products[i] for i in order if products[i]["name"]][:max_results]

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
    """Extract a product's name, price range and per-merchant offers.

    Reads the page's JSON-LD ``ProductGroup``, picking the ``hasVariant`` entry
    matching ``product_id`` (the page shows the whole group even when a single
    variant was requested). Raises `ParseError` if the structured data is
    missing — silently degrading to an empty offer list would present a broken
    fetch as a product with no merchants.
    """
    soup = BeautifulSoup(html, "lxml")
    product = _find_product_ld(soup, product_id)

    if product is None:
        raise ParseError(
            f"product page for {product_id} has no JSON-LD Product or "
            "ProductGroup; selectors may have drifted or an error page "
            "was served"
        )

    agg = product.get("offers") or {}
    if isinstance(agg, list):
        agg = agg[0] if agg else {}
    offers = _sellers(agg)

    return {
        "id": product_id,
        "name": product.get("name"),
        "url": product.get("url") or f"{BASE_URL}a{product_id}.html",
        "price_min": _num(agg.get("lowPrice")),
        "price_max": _num(agg.get("highPrice")),
        "currency": agg.get("priceCurrency", "EUR"),
        "offer_count": _num(agg.get("offerCount")) or (len(offers) or None),
        "offers": offers,
    }


def _find_product_ld(soup: "BeautifulSoup", pid: str) -> dict[str, Any] | None:
    """Find the JSON-LD Product for `pid` — direct, or a ProductGroup variant."""
    blocks = _jsonld_blocks(soup)
    for block in blocks:
        if block.get("@type") == "Product" and _url_id(block.get("url")) in (pid, None):
            return block
    for block in blocks:
        if block.get("@type") == "ProductGroup":
            for variant in block.get("hasVariant") or []:
                if _url_id(variant.get("url")) == pid:
                    return variant
    # Last resort: any Product-shaped block on the page.
    for block in blocks:
        if block.get("@type") == "Product":
            return block
    return None


def _sellers(aggregate: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-merchant offers from an AggregateOffer's nested `offers` list."""
    out: list[dict[str, Any]] = []
    for offer in aggregate.get("offers") or []:
        if not isinstance(offer, dict):
            continue
        seller = offer.get("seller") or {}
        out.append(
            {
                "merchant": seller.get("name") if isinstance(seller, dict) else None,
                "price": _num(offer.get("price")),
                "currency": offer.get("priceCurrency", "EUR"),
                "url": offer.get("url"),
                "availability": _short_avail(offer.get("availability")),
            }
        )
    out.sort(key=lambda o: (o["price"] is None, o["price"]))
    return out


def _jsonld_blocks(soup: "BeautifulSoup") -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        blocks.extend(data if isinstance(data, list) else [data])
    return blocks


def _short_avail(value: Any) -> str | None:
    """'https://schema.org/InStock' -> 'InStock'."""
    if not isinstance(value, str):
        return None
    return value.rsplit("/", 1)[-1] or None


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
