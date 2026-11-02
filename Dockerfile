# Pin by digest for reproducible builds, e.g.
#   FROM python:3.12-slim@sha256:<digest>
FROM python:3.12-slim

# Pillow downscales phone photos before they are stored or sent to a provider.
# cryptography verifies RS256 OIDC ID tokens (Pocket ID / Authelia default) and
# encrypts the login state; without it OIDC logins fail closed.
RUN pip install --no-cache-dir Pillow==12.3.0 cryptography==50.0.2

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin macrolog \
 && mkdir -p /data && chown macrolog:macrolog /data

WORKDIR /app
# Code only: never bake .env, data/, tls/ or .git into an image (see .dockerignore).
COPY server.py ./
COPY app ./app
COPY static ./static

ENV MACROLOG_HOST=0.0.0.0 \
    MACROLOG_PORT=8765 \
    MACROLOG_DATA=/data \
    PYTHONUNBUFFERED=1

# The server refuses to listen on 0.0.0.0 without login. Configure OIDC_* (see
# .env.example), or run a single-user instance with -e MACROLOG_ALLOW_NO_AUTH=1
# and publish it on loopback only: -p 127.0.0.1:8765:8765
USER macrolog
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python3 -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ['MACROLOG_PORT']+'/healthz',timeout=4)" || exit 1

CMD ["python3", "server.py"]
