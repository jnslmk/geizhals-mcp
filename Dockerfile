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
    && apt-get install -y --no-install-recommends xvfb ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install deps before app code so a code change does not redo the ~400 MB
# Chromium download.
COPY pyproject.toml README.md ./
COPY geizhals_mcp ./geizhals_mcp
RUN pip install . \
    && patchright install --with-deps chromium

# Chromium lives in PLAYWRIGHT_BROWSERS_PATH, which root just wrote to; hand it
# to the unprivileged user the container actually runs as.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin mcp \
    && chown -R mcp:mcp /opt/playwright
USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status == 200 else 1)"

# xvfb-run gives Chromium a virtual display so it can launch headed.
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1366x900x24", "geizhals-mcp"]
