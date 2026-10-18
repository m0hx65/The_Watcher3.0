# wireproxy — userspace WireGuard exposed as a local SOCKS5 proxy, for the
# page tunnel (app/monitor/page_tunnel.py). Built from source at a pinned
# version; `go install` checks the module against Go's checksum database
# (sum.golang.org), so nothing here is an unverified download. Static
# (CGO_ENABLED=0), so it runs on the slim image below as is.
FROM golang:1.26 AS wireproxy
RUN CGO_ENABLED=0 GOBIN=/out go install github.com/windtf/wireproxy/cmd/wireproxy@v1.1.3

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
# The home fetcher's script, so the phone can update itself from the bot
# (GET /home-fetch/worker.py) when it cannot reach GitHub.
COPY tools/home_fetcher/home_fetcher.py ./tools/home_fetcher/home_fetcher.py
COPY --from=wireproxy /out/wireproxy /usr/local/bin/wireproxy

RUN mkdir -p /app/data/media

ENV PORT=8000 \
    MEDIA_DIR=/app/data/media

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/health || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
