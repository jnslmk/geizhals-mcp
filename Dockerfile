FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# xvfb: Geizhals' Cloudflare wall is far easier to clear with a *headed* browser,
# so the container runs Chromium under a virtual display rather than headless.
# The rest are Chromium's runtime shared-library dependencies (installed via
# `patchright install --with-deps` below, but the apt lists are needed first).
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Browser first, in a layer that depends on nothing that changes: a ~400 MB
# Chromium download must not be redone every time this project's own code
# changes. patchright is pinned loosely here and re-pinned by pyproject below.
RUN pip install "patchright>=1.49" \
    && patchright install --with-deps chromium

# App code last so edits only invalidate these two cheap layers.
COPY pyproject.toml README.md ./
COPY geizhals_mcp ./geizhals_mcp
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN pip install . \
    && chmod 0755 /usr/local/bin/entrypoint.sh

# Chromium lives in PLAYWRIGHT_BROWSERS_PATH, which root just wrote to; hand it
# to the unprivileged user the container actually runs as.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin mcp \
    && chown -R mcp:mcp /opt/playwright
USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status == 200 else 1)"

# entrypoint.sh starts Xvfb deterministically (no xvfb-run signal race) and
# then execs the server as PID 1.
CMD ["/usr/local/bin/entrypoint.sh"]
