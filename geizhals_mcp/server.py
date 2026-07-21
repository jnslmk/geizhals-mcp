"""MCP server exposing Geizhals price-comparison search as tools.

Mirrors the shape of kleinanzeigen-mcp: a `search_*` tool returns lightweight
summaries with product ids, and a `get_product` / `get_products_batch` pair
fetches the full per-merchant offer list for the ids worth a closer look. This
keeps a broad search from flooding the model's context with offer tables it did
not ask for.

The scraping itself (headless Chromium past Cloudflare + HTML parsing) lives in
`browser.py` and `scraper.py`; this module owns the browser lifecycle and the
tool surface only.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

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


def _manager() -> BrowserManager:
    if _browser is None:  # pragma: no cover - guarded by the lifespan
        raise RuntimeError("Browser manager is not running")
    return _browser


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
    version="0.1.0",
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
    min_price: Annotated[int | None, Field(description="Minimum price in EUR", ge=0)] = None,
    max_price: Annotated[int | None, Field(description="Maximum price in EUR", ge=0)] = None,
    max_results: Annotated[
        int, Field(description="Maximum product summaries to return", ge=1)
    ] = 20,
) -> dict[str, Any]:
    """Search Geizhals for products matching a keyword.

    Returns product summaries — id, name, current best price and how many
    merchants offer it. Use `get_products_batch` with the returned ids to get
    the full per-merchant offer list. `min_price`/`max_price` filter the
    returned summaries; they do not change Geizhals' own ranking.
    """
    sort_code = {"price": "p", "relevance": "r"}.get(sort, "r")
    url = scraper.search_url(query, sort=sort_code)
    html = await _fetch(url)

    results = scraper.parse_search(html, max_results=min(max_results, MAX_RESULTS))
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
    """Fetch one product's full detail: name, price range and per-merchant offers.

    For more than one product prefer `get_products_batch`, which reuses the
    browser and reports failures per id instead of failing the whole call.
    """
    product_id = product_id.strip()
    if not product_id.isdigit():
        raise ValueError("product_id must be the numeric id from an aN.html URL")

    html = await _fetch(f"https://geizhals.de/a{product_id}.html")
    return scraper.parse_product(html, product_id)


@mcp.tool
async def get_products_batch(
    product_ids: Annotated[
        list[str],
        Field(description="Product ids to fetch, typically taken from a search"),
    ],
    max_concurrent: Annotated[
        int, Field(description="Detail pages to fetch in parallel", ge=1, le=3)
    ] = 2,
) -> dict[str, Any]:
    """Fetch full offer lists for several products in one call.

    The normal follow-up to `search_products`. Failed ids are reported in
    `errors` rather than failing the whole call. Keep `max_concurrent` low —
    Geizhals is behind Cloudflare and throttles aggressive parallel access.
    """
    ids = [i.strip() for i in product_ids if i and i.strip().isdigit()]
    if not ids:
        raise ValueError("product_ids must contain at least one numeric id")
    if len(ids) > MAX_BATCH_SIZE:
        raise ValueError(
            f"Too many ids ({len(ids)}); fetch at most {MAX_BATCH_SIZE} per call"
        )

    semaphore = asyncio.Semaphore(max_concurrent)

    async def fetch(pid: str) -> dict[str, Any]:
        async with semaphore:
            html = await _fetch(f"https://geizhals.de/a{pid}.html")
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
        "success": True,
        "requested": len(ids),
        "returned": len(results),
        "results": results,
        "errors": errors,
    }


@mcp.tool
async def search_by_url(
    url: Annotated[str, Field(description="A geizhals.de/.at/.eu search or category URL")],
    max_results: Annotated[
        int, Field(description="Maximum product summaries to return", ge=1)
    ] = 20,
) -> dict[str, Any]:
    """Search using a Geizhals URL, preserving all of its filters.

    Use this when the user pastes a Geizhals link — a filtered category URL
    encodes constraints (attributes, price bands, availability) that the
    keyword `search_products` cannot express, and this keeps every one of them.
    """
    if "geizhals." not in url:
        raise ValueError("url must be a geizhals.de/.at/.eu URL")
    html = await _fetch(url)
    results = scraper.parse_search(html, max_results=min(max_results, MAX_RESULTS))
    return {"url": url, "returned": len(results), "results": results}


async def _fetch(url: str) -> str:
    """Fetch rendered HTML, mapping a Cloudflare block onto a clean tool error."""
    try:
        return await _manager().fetch_html(url)
    except CloudflareBlocked as exc:
        raise RuntimeError(str(exc)) from exc


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
