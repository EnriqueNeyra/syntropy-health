# Syntropy Health — production image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SYNTROPY_DATA_DIR=/data \
    SYNTROPY_INSTALL=docker

RUN groupadd -g 10001 syntropy && useradd -u 10001 -g syntropy -m -s /usr/sbin/nologin syntropy \
    && mkdir -p /data && chown syntropy:syntropy /data

# Tesseract reads photos and scans of paper lab reports on this machine.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies first, read from pyproject.toml, so code changes don't reinstall them.
COPY pyproject.toml .
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/requirements.txt && pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY app/ ./app/
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# The entrypoint starts as root only to pick the data directory's owner, then drops to that
# user (or "syntropy" for a fresh volume) before the server starts.
ENTRYPOINT ["docker-entrypoint.sh"]
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# --proxy-headers lets the app build correct OAuth redirect URLs behind Tailscale Serve,
# Caddy, nginx or Cloudflare Tunnel. FORWARDED_ALLOW_IPS limits which proxies are trusted.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
