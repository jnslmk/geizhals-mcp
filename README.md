# geizhals-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an LLM search
[Geizhals](https://geizhals.de), a leading German/DACH price-comparison site,
for the cheapest offers on new products. Built for self-hosting: one container,
no API keys, no per-query credits.

Companion to [jnslmk/kleinanzeigen-mcp](https://github.com/jnslmk/kleinanzeigen-mcp)
— that one covers second-hand classifieds, this one covers new retail goods.

> Geizhals sits behind a Cloudflare JS challenge, so this drives a real
> (patched, headed) browser to reach the page. Search parsing is anchored on the
> live `galleryview__*` markup; product detail is read from the page's JSON-LD
> (`ProductGroup` → variant `AggregateOffer`), which carries the per-merchant
> offers directly. Both paths were verified end-to-end against live pages
> (July 2026).
>
> **Note on sorting:** the default `relevance` sort returns the products you
> searched for. `sort=price` asks Geizhals for cheapest-first across its broad
> free-text match, which for loose queries surfaces cheap loosely-related items —
> prefer it only with a specific query.

## Tools

| Tool | What it does |
|------|--------------|
| `search_products` | Search by keyword, sort by price/relevance, filter by price band. Returns product summaries. |
| `get_product` | Full detail — name, price range, per-merchant offers — for one product id. |
| `get_products_batch` | Full details for several ids at once — the normal follow-up to a search. |
| `search_by_url` | Search from a pasted Geizhals URL, preserving filters `search_products` cannot express. |

The intended flow is `search_products` → pick interesting ids →
`get_products_batch`. Search results omit the per-merchant offer tables so a
broad search does not blow up the model's context window.

## How it works

Geizhals has no public API and is fronted by Cloudflare's "Sichere Verbindung
wird überprüft" JS challenge, which returns 403 to any plain HTTP client. So:

- **Browser:** [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)
  (a patched, undetected Playwright fork) drives Chromium **headed under Xvfb** —
  a headed browser behind a virtual display clears Cloudflare far more reliably
  than headless from a datacenter IP.
- **Parsing:** `scraper.py` prefers the page's `application/ld+json` Product
  block and the stable `aNNNNNNN.html` product hrefs, falling back to CSS
  selectors (collected in `_SEL`) for the human-readable fields.

## Running it

```bash
docker run --rm -p 8000:8000 --shm-size=1g ghcr.io/jnslmk/geizhals-mcp:latest
```

Streamable HTTP at `http://localhost:8000/mcp`, plain `GET /healthz` for
container healthchecks. Give the container at least 1.5 GB of memory and
`--shm-size=1g` — Chromium maps its renderer heap into `/dev/shm`.

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `MCP_TRANSPORT` | `http` | `http` (streamable HTTP) or `stdio` |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `8000` | Bind port |
| `MCP_PATH` | `/mcp` | MCP endpoint path |
| `GH_MAX_CONCURRENT` | `2` | Concurrent browser contexts / scrapes |
| `GH_MAX_RESULTS` | `40` | Hard cap on products returned by a search |
| `GH_MAX_BATCH_SIZE` | `10` | Cap on ids per `get_products_batch` call |
| `GH_CHALLENGE_TIMEOUT_MS` | `25000` | How long to wait for Cloudflare to clear |
| `GH_HEADLESS` | `0` | `1` runs headless (local dev without a display) |
| `LOG_LEVEL` | `INFO` | Python log level |

### LibreChat

```yaml
mcpServers:
  geizhals:
    type: streamable-http
    url: "http://geizhals-mcp:8000/mcp"
    timeout: 180000
    chatMenu: true

mcpSettings:
  allowedAddresses:
    - "geizhals-mcp:8000"
```

`allowedAddresses` is required — LibreChat's SSRF guard blocks MCP URLs that
resolve to private addresses, which a sibling container always does. The timeout
is generous because a cold call starts a browser and waits out a Cloudflare
challenge.

### Claude Code

```bash
claude mcp add --transport http geizhals http://localhost:8000/mcp
```

## Development

```bash
uv venv && uv pip install -e .
patchright install chromium
# Headless is fine for a quick check; headed (default) needs a display:
GH_HEADLESS=1 python -m geizhals_mcp
```

## Verifying the scraper

The extraction was written without live-HTML access. Before trusting a release:

1. Run the server and call `search_products` with a common term (e.g. `RTX 4070`).
2. If results come back empty but the logs show no Cloudflare block, the row
   selectors in `_SEL` are stale — capture a real search page
   (`page.content()`), inspect it, and correct `_SEL["row"]`, `_SEL["row_price"]`
   and `_SEL["row_offercount"]`.
3. Do the same for `get_product` against `_SEL["offer_*"]`.
4. If calls fail with a Cloudflare-timeout error, the datacenter IP is flagged —
   route egress through a residential/mobile proxy (see below).

## Cloudflare and proxies

Cloudflare challenges are much harsher on datacenter IPs. If the headed-Xvfb
browser cannot clear them from your host, point Chromium at a residential/mobile
egress proxy. `browser.py` is the single place to add a `proxy=` argument to
`chromium.launch`.

## Images

`ghcr.io/jnslmk/geizhals-mcp` — multi-arch (`linux/amd64`, `linux/arm64`), built
by GitHub Actions on native runners for each architecture.

| Tag | Meaning |
|-----|---------|
| `latest` | Newest build of `main` |
| `sha-<full-sha>` | A specific commit |
| `v1.2.3`, `v1.2` | Release tags |

## Caveats

Geizhals has no public API, so this scrapes the site with a headless browser.
It can break whenever Geizhals change their markup or tighten Cloudflare, and
heavy or parallel use will trip bot detection — particularly from a datacenter
IP. Scraping is also at odds with Geizhals' terms of service. Keep it to
personal-scale use; the conservative concurrency defaults exist for exactly
this reason.

## License

MIT — see [LICENSE](LICENSE).
