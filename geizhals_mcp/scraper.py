"""Parse Geizhals search and product HTML into plain dicts.

Geizhals has no public API, so this parses rendered HTML fetched by
`browser.py`. Two things keep it from being brittle:

1. **JSON-LD first.** Geizhals product pages embed a `<script
   type="application/ld+json">` Product block. Reading price/name/offer-count
   from that survives CSS-class renames, which are the usual reason a scraper
   silently breaks.
2. **Regex on product hrefs.** Every Geizhals product lives at `aNNNNNNN.html`;
   pulling ids out of the anchors is robust even when the surrounding markup
   changes. The human-readable fields (name, price) are then read defensively —
   a missed selector degrades one field to `None`, it does not crash the call.

⚠️  SELECTORS ARE UNVERIFIED against live HTML — this repo was scaffolded while
Geizhals' Cloudflare wall blocked inspection. Everything class-name-based below
is a best-effort guess collected in `_SEL`; smoke-test each tool and correct
`_SEL` before trusting a released version. The JSON-LD and href paths are the
parts meant to hold.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup, Tag

BASE_URL = "https://geizhals.de/"

# Product id -> canonical URL. `aNNNNNNN.html` is Geizhals' permanent product URL
# shape and the one stable anchor in the whole page.
_PRODUCT_HREF = re.compile(r"/a(\d+)\.html")
_PRICE = re.compile(r"(\d[\d.\s]*),(\d{2})")

# All fragile, class-name-based selectors live here so a markup change is a
# one-place fix. Each is a CSS selector tried in order; first hit wins.
_SEL: dict[str, tuple[str, ...]] = {
    "row": ("div.productlist__item", "div.cat__product", "article"),
    "row_price": (".productlist__price", ".gh_price", "[class*='price']"),
    "row_offercount": (".productlist__offercount", "[class*='offercount']"),
    "product_title": ("h1.variant__header__headline", "h1"),
    "offer_row": ("div.offerlist__row", ".offer__row", "tr.row"),
    "offer_merchant": (".offer__merchant-name", ".merchant", "[class*='merchant']"),
    "offer_price": (".offer__price", ".gh_price", "[class*='price']"),
    "offer_availability": (".offer__delivery", ".availability", "[class*='delivery']"),
}


def search_url(query: str, *, sort: str | None = None, region: str = "de") -> str:
    """Build a Geizhals free-text search URL."""
    url = f"{BASE_URL}?fs={quote_plus(query)}&hloc={region}"
    if sort:
        # p = cheapest first, r = relevance/rank. Anything else Geizhals ignores.
        url += f"&sort={sort}"
    return url


def _first(node: Tag, keys: tuple[str, ...]) -> Tag | None:
    for sel in keys:
        found = node.select_one(sel)
        if found is not None:
            return found
    return None


def _to_eur(text: str | None) -> float | None:
    """Parse a German-formatted price ('1.299,00 €') into a float."""
    if not text:
        return None
    m = _PRICE.search(text)
    if not m:
        return None
    whole = m.group(1).replace(".", "").replace(" ", "")
    return float(f"{whole}.{m.group(2)}")


def _jsonld_blocks(soup: BeautifulSoup) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        blocks.extend(data if isinstance(data, list) else [data])
    return blocks


def _product_jsonld(soup: BeautifulSoup) -> dict[str, Any] | None:
    for block in _jsonld_blocks(soup):
        if block.get("@type") == "Product":
            return block
    return None


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #


def parse_search(html: str, *, max_results: int) -> list[dict[str, Any]]:
    """Extract product summaries from a search results page.

    Anchored on the `aNNNNNNN.html` hrefs (stable); name and price are read from
    the surrounding row defensively. Deduplicated by product id, preserving the
    page's own ranking order.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=_PRODUCT_HREF):
        m = _PRODUCT_HREF.search(anchor.get("href", ""))
        if not m:
            continue
        pid = m.group(1)
        if pid in seen:
            continue

        name = (anchor.get("title") or anchor.get_text(strip=True) or "").strip()
        if not name:
            continue  # image-only or utility anchors to the same product
        seen.add(pid)

        row = _enclosing_row(anchor)
        price = None
        offers = None
        if row is not None:
            price = _to_eur(_text_of(_first(row, _SEL["row_price"])))
            offers = _int_of(_text_of(_first(row, _SEL["row_offercount"])))

        results.append(
            {
                "id": pid,
                "name": name,
                "price": price,
                "currency": "EUR",
                "offer_count": offers,
                "url": urljoin(BASE_URL, f"a{pid}.html"),
            }
        )
        if len(results) >= max_results:
            break

    return results


def _enclosing_row(anchor: Tag) -> Tag | None:
    """Walk up to the product row so we can read its price/offer-count."""
    for sel in _SEL["row"]:
        row = anchor.find_parent(_row_matcher(sel))
        if row is not None:
            return row
    return anchor.parent


def _row_matcher(sel: str):
    # find_parent needs a predicate; translate the simple "tag.class" selectors
    # in _SEL into one. Anything fancier falls back to a tag-name match.
    if "." in sel:
        tag, _, cls = sel.partition(".")
        return lambda t: t.name == (tag or t.name) and cls in (t.get("class") or [])
    return sel


# --------------------------------------------------------------------------- #
# product detail
# --------------------------------------------------------------------------- #


def parse_product(html: str, product_id: str) -> dict[str, Any]:
    """Extract a product's name, aggregate price and per-merchant offers."""
    soup = BeautifulSoup(html, "lxml")
    ld = _product_jsonld(soup) or {}

    name = ld.get("name") or _text_of(_first(soup, _SEL["product_title"]))
    aggregate = _aggregate_from_ld(ld)
    offers = _parse_offers(soup)

    return {
        "id": product_id,
        "name": name,
        "url": urljoin(BASE_URL, f"a{product_id}.html"),
        "price_min": aggregate.get("low"),
        "price_max": aggregate.get("high"),
        "currency": aggregate.get("currency", "EUR"),
        "offer_count": aggregate.get("count", len(offers) or None),
        "offers": offers,
    }


def _aggregate_from_ld(ld: dict[str, Any]) -> dict[str, Any]:
    offers = ld.get("offers") or {}
    if isinstance(offers, list):  # some pages emit a list of Offer, not Aggregate
        prices = [
            float(o["price"])
            for o in offers
            if isinstance(o, dict) and _is_number(o.get("price"))
        ]
        return {
            "low": min(prices) if prices else None,
            "high": max(prices) if prices else None,
            "count": len(offers) or None,
            "currency": _first_currency(offers),
        }
    return {
        "low": _num(offers.get("lowPrice")),
        "high": _num(offers.get("highPrice")),
        "count": _num(offers.get("offerCount")),
        "currency": offers.get("priceCurrency", "EUR"),
    }


def _parse_offers(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Per-merchant offers from the offer list. Best-effort, per-row isolated."""
    offers: list[dict[str, Any]] = []
    for sel in _SEL["offer_row"]:
        rows = soup.select(sel)
        if rows:
            for row in rows:
                offer = {
                    "merchant": _text_of(_first(row, _SEL["offer_merchant"])),
                    "price": _to_eur(_text_of(_first(row, _SEL["offer_price"]))),
                    "availability": _text_of(_first(row, _SEL["offer_availability"])),
                }
                if offer["merchant"] or offer["price"] is not None:
                    offers.append(offer)
            break  # first selector that matched rows wins
    return offers


# --------------------------------------------------------------------------- #
# tiny helpers
# --------------------------------------------------------------------------- #


def _text_of(node: Tag | None) -> str | None:
    if node is None:
        return None
    text = node.get_text(" ", strip=True)
    return text or None


def _int_of(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def _num(value: Any) -> float | None:
    return float(value) if _is_number(value) else None


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _first_currency(offers: list[Any]) -> str:
    for o in offers:
        if isinstance(o, dict) and o.get("priceCurrency"):
            return o["priceCurrency"]
    return "EUR"
