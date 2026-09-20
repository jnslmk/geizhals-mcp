"""MCP server exposing Geizhals price-comparison search as tools.

Mirrors the shape of kleinanzeigen-mcp: a `search_*` tool returns lightweight
summaries with product ids, and a `get_product` / `get_products_batch` pair
fetches the full per-merchant offer list for the ids worth a closer look. This
keeps a broad search from flooding the model's context with offer tables it did
not ask for.

The browser-backed search scraping lives in `browser.py`; direct product-page
fetching and HTML parsing live in `scraper.py`. This module owns the browser
lifecycle and tool surface only.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from geizhals_mcp import scraper
from geizhals_mcp.browser import BrowserManager, CloudflareBlocked

log = logging.getLogger("geizhals-mcp")

# Search responses go straight into an LLM context window, so cap how much a
# single call can return. Detail fetches each open a page, so cap the batch too.
MAX_RESULTS = int(os.getenv("GH_MAX_RESULTS", "40"))
MAX_BATCH_SIZE = int(os.getenv("GH_MAX_BATCH_SIZE", "10"))

_browser: BrowserManager | None = None


# Hostnames `search_by_url` may point the browser at: only Geizhals' own
# (www.) domains count — the previous substring check let
# "https://evil.example/?x=geizhals.de" through.
_ALLOWED_GH_HOSTS = frozenset(
    host
    for tld in ("de", "at", "eu")
    for host in (f"geizhals.{tld}", f"www.geizhals.{tld}")
)


def _validate_scrape_url(url: str) -> None:
    """Reject any URL we do not want the real browser to navigate to."""
    parts = urlparse(url)
    try:
        port = parts.port
    except ValueError:  # malformed port (e.g. outside 0-65535)
        port = -1
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or (parts.hostname or "").lower() not in _ALLOWED_GH_HOSTS
    ):
        raise ValueError("url must be a https geizhals.de/.at/.eu URL")


def _manager() -> BrowserManager:
    if _browser is None:  # pragma: no cover - guarded by the lifespan
        raise RuntimeError("Browser manager is not running")
    return _browser


def _coerce_int(
    value: str | int | None, field: str, *, ge: int | None = None
) -> int | None:
    """Coerce the numeric strings LLMs routinely send for int parameters.

    FastMCP validates tool input against the JSON schema before the function
    runs, so a parameter typed ``int`` rejects the string ``"600"`` outright
    (the same bug kleinanzeigen-mcp 0.1.1 fixed for its price params).
    Accepting ``str | int`` in the schema and normalising here keeps the
    model-facing contract lenient while the scraper still sees a real int.
    """
    if value is None or isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field} must be an integer, got {value!r}") from exc
    else:
        raise ValueError(f"{field} must be an integer, got {value!r}")
    if ge is not None and result is not None and result < ge:
        raise ValueError(f"{field} must be >= {ge}, got {result}")
    return result


def _resolve_max_results(
    max_results: str | int | None, limit: str | int | None
) -> int:
    """Accept either name for the result-cap knob, on both search tools.

    Across the sibling product-search MCP servers this knob has two names —
    ``max_results`` here and in baumarkt-mcp, ``limit`` in aliexpress-mcp,
    ebay-mcp and amazon-mcp — and one LLM sees all of them in a single
    conversation. FastMCP emits ``additionalProperties: false``, so a model
    that carried ``limit`` over from a sibling server got a hard schema
    rejection, and LibreChat's rejection message names no field ("Additional
    properties are not allowed"), so the model cannot see what to fix and can
    only guess (mirrors the ``page_count``/``max_pages`` split kleinanzeigen-mcp
    hit for the same reason).

    ``max_results`` stays canonical rather than being renamed, to avoid churn
    for existing callers; ``limit`` is accepted as a declared, deprecated
    alias rather than silently swallowed — an unknown key that is quietly
    ignored would hand back the default 20 results while the model believed
    it had asked for more.
    """
    if max_results is not None and limit is not None:
        resolved = _coerce_int(max_results, "max_results", ge=1)
        alias = _coerce_int(limit, "limit", ge=1)
        if resolved != alias:
            raise ValueError(
                "max_results and limit are two names for the same parameter "
                f"but were given different values ({resolved} and {alias}); "
                "pass max_results only"
            )
    elif limit is not None:
        resolved = _coerce_int(limit, "limit", ge=1)
    else:
        resolved = _coerce_int(max_results, "max_results", ge=1)
    return min(resolved or 20, MAX_RESULTS)


# Shared by both search tools so the pair can never drift apart again. The
# alias is declared in the schema rather than silently swallowed, for the
# same reason as `_resolve_max_results` above.
_MaxResults = Annotated[
    str | int | None,
    Field(description="Maximum product summaries to return (default 20)"),
]
_LimitAlias = Annotated[
    str | int | None,
    Field(description="Deprecated alias for `max_results`; prefer `max_results`"),
]


@asynccontextmanager
async def lifespan(_: FastMCP) -> AsyncIterator[None]:
    """Start one shared browser for the process lifetime."""
    global _browser
    _browser = BrowserManager()
    await _browser.start()
    try:
        yield
    finally:
        await _browser.close()
        _browser = None


mcp = FastMCP(
    name="geizhals",
    version="0.1.5",  # x-release-please-version
    lifespan=lifespan,
    instructions=(
        "Search Geizhals, a leading German/DACH price-comparison site, for the "
        "cheapest offers on new products (electronics, hardware, appliances, "
        "and more). Start with `search_products` to get product ids and the "
        "current best price, then call `get_products_batch` for the full list "
        "of merchants and their prices on the products worth comparing. Prices "
        "are in EUR. This covers *new* retail goods — for second-hand listings "
        "use the kleinanzeigen tools instead."
    ),
)


@mcp.tool
async def search_products(
    query: Annotated[
        str,
        Field(description="Product search terms, e.g. 'RTX 4070' or 'Bosch Waschmaschine'"),
    ],
    sort: Annotated[
        str,
        Field(description="'price' for cheapest first, or 'relevance' (default)"),
    ] = "relevance",
    min_price: Annotated[
        str | int | None, Field(description="Minimum price in EUR")
    ] = None,
    max_price: Annotated[
        str | int | None, Field(description="Maximum price in EUR")
    ] = None,
    max_results: _MaxResults = None,
    limit: _LimitAlias = None,
) -> dict[str, Any]:
    """Search Geizhals for products matching a keyword.

    Returns product summaries — id, name, current best price and how many
    merchants offer it. Use `get_products_batch` with the returned ids to get
    the full per-merchant offer list. `min_price`/`max_price` filter the
    returned summaries; they do not change Geizhals' own ranking.
    """
    sort_code = {"price": "p", "relevance": "r"}.get(sort, "r")
    url = scraper.search_url(query, sort=sort_code)

    min_price = _coerce_int(min_price, "min_price", ge=0)
    max_price = _coerce_int(max_price, "max_price", ge=0)
    max_results = _resolve_max_results(max_results, limit)

    html = await _fetch(url)

    results = scraper.parse_search(html, max_results=max_results)
    if min_price is not None:
        results = [r for r in results if r["price"] is None or r["price"] >= min_price]
    if max_price is not None:
        results = [r for r in results if r["price"] is None or r["price"] <= max_price]

    return {"query": query, "returned": len(results), "results": results}


@mcp.tool
async def get_product(
    product_id: Annotated[
        str,
        Field(description="Geizhals numeric product id, e.g. '2830710' (the N in aN.html)"),
    ],
) -> dict[str, Any]:
    """Fetch one product's full detail: name, price range and merchant offers.

    For more than one product prefer `get_products_batch`, which reports
    failures per id instead of failing the whole call.
    """
    product_id = product_id.strip()
    if not product_id.isdigit():
        raise ValueError("product_id must be the numeric id from an aN.html URL")

    html = await _fetch_product(f"https://geizhals.de/a{product_id}.html")
    return scraper.parse_product(html, product_id)


@mcp.tool
async def get_products_batch(
    product_ids: Annotated[
        list[str],
        Field(description="Product ids to fetch, typically taken from a search"),
    ],
    max_concurrent: Annotated[
        str | int, Field(description="Detail pages to fetch in parallel")
    ] = 2,
) -> dict[str, Any]:
    """Fetch full offer lists for several products in one call.

    The normal follow-up to `search_products`. Failed ids are reported in
    `errors` rather than failing the whole call; `success` is only true when
    every id came back. `max_concurrent` limits direct detail fetches inside
    this call.
    """
    ids = [i.strip() for i in product_ids if i and i.strip().isdigit()]
    if not ids:
        raise ValueError("product_ids must contain at least one numeric id")
    if len(ids) > MAX_BATCH_SIZE:
        raise ValueError(
            f"Too many ids ({len(ids)}); fetch at most {MAX_BATCH_SIZE} per call"
        )
    max_concurrent = min(_coerce_int(max_concurrent, "max_concurrent", ge=1) or 2, 3)

    semaphore = asyncio.Semaphore(max_concurrent)

    async def fetch(pid: str) -> dict[str, Any]:
        async with semaphore:
            html = await _fetch_product(f"https://geizhals.de/a{pid}.html")
            return scraper.parse_product(html, pid)

    outcomes = await asyncio.gather(*(fetch(i) for i in ids), return_exceptions=True)

    results: list[Any] = []
    errors: list[dict[str, str]] = []
    for pid, outcome in zip(ids, outcomes):
        if isinstance(outcome, BaseException):
            errors.append({"id": pid, "error": str(outcome)})
        else:
            results.append(outcome)

    return {
        "success": not errors,
        "requested": len(ids),
        "returned": len(results),
        "results": results,
        "errors": errors,
    }


@mcp.tool
async def search_by_url(
    url: Annotated[str, Field(description="A geizhals.de/.at/.eu search or category URL")],
    max_results: _MaxResults = None,
    limit: _LimitAlias = None,
) -> dict[str, Any]:
    """Search using a Geizhals URL, preserving all of its filters.

    Use this when the user pastes a Geizhals link — a filtered category URL
    encodes constraints (attributes, price bands, availability) that the
    keyword `search_products` cannot express, and this keeps every one of them.
    """
    _validate_scrape_url(url)
    max_results = _resolve_max_results(max_results, limit)
    html = await _fetch(url)
    results = scraper.parse_search(html, max_results=max_results)
    return {"url": url, "returned": len(results), "results": results}


async def _fetch(url: str) -> str:
    """Fetch rendered HTML, mapping a Cloudflare block onto a clean tool error."""
    try:
        return await _manager().fetch_html(url)
    except CloudflareBlocked as exc:
        raise RuntimeError(str(exc)) from exc


async def _fetch_product(url: str) -> str:
    """Fetch one product page without browser state or challenge handling."""
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    """Container healthcheck: reports whether the browser actually came up."""
    if _browser is None or not _browser.ready:
        return JSONResponse({"status": "starting"}, status_code=503)
    return JSONResponse({"status": "ok"})


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    transport = os.getenv("MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="http",
            host=os.getenv("MCP_HOST", "0.0.0.0"),  # noqa: S104 - containerised
            port=int(os.getenv("MCP_PORT", "8000")),
            path=os.getenv("MCP_PATH", "/mcp"),
        )


if __name__ == "__main__":
    main()
