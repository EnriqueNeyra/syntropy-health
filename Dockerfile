# Syntropy Health — production image
# Base images are pinned by digest, so a release is rebuilt from what it was tested on. Dependabot proposes new digests
# weekly, and docker-refresh.yml rebuilds the latest release on them. uv's version moves with the installers' (UV_VERSION).
FROM ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 AS uv

# The exact versions in uv.lock, with their hashes, for this image's install below.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS locked
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-header -o /requirements.txt

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SYNTROPY_DATA_DIR=/data \
    SYNTROPY_INSTALL=docker

RUN groupadd -g 10001 syntropy && useradd -u 10001 -g syntropy -m -s /usr/sbin/nologin syntropy \
    && mkdir -p /data && chown syntropy:syntropy /data

# Tesseract reads photos and scans of paper lab reports on this machine. The upgrade brings in the distribution's
# security fixes from after the base image was built.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY --from=locked /requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes --no-deps -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY LICENSE NOTICE ./
COPY app/ ./app/
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY scripts/docker-syntropy-health.sh /usr/local/bin/syntropy-health

# The entrypoint starts as root only to pick the data directory's owner, then drops to that
# user (or "syntropy" for a fresh volume) before the server starts.
ENTRYPOINT ["docker-entrypoint.sh"]
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# --proxy-headers lets the app build correct OAuth redirect URLs behind Tailscale Serve,
# Caddy, nginx or Cloudflare Tunnel. FORWARDED_ALLOW_IPS limits which proxies are trusted: a proxy in another container
# has to be listed there, or every visitor looks like that container's address (the server then treats them as far away).
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
