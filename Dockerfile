# Two stages: build the database from CSVs once, then ship a slim runtime.
#
# Sprites are NOT baked in — 754 MB in an image you redeploy is 754 MB of waste.
# Mount them, or serve /sprites from object storage behind a CDN and point the
# frontend at it. The database is small enough (80 MB) to bake.

# ---------- stage 1: build the Pokedex ----------
FROM python:3.13-slim AS dbbuild
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY build_db.py ./
# Pull just the CSV directory; the full repo is far larger.
RUN git clone --depth 1 --filter=blob:none --sparse \
        https://github.com/PokeAPI/pokeapi.git _src \
    && git -C _src sparse-checkout set data/v2/csv \
    && mkdir -p data/csv && mv _src/data/v2/csv/*.csv data/csv/ && rm -rf _src
RUN python build_db.py

# ---------- stage 2: runtime ----------
FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY config.py security.py observability.py pokedb.py sprites.py agent.py server.py build_db.py ./
COPY scripts/ ./scripts/
COPY static/ ./static/
COPY --from=dbbuild /build/data/pokedex.sqlite ./data/pokedex.sqlite

# Sprites are NOT shipped: 754 MB of PNGs in a redeployed image is 754 MB of
# waste. SPRITE_SOURCE=proxy serves them from our own origin on demand instead,
# so the CSP stays at `img-src 'self'`. Mount assets/sprites to go back to local.
ENV SPRITE_SOURCE=proxy
RUN mkdir -p assets/sprites data

# Run unprivileged. /app stays read-only in practice: the DB is opened mode=ro
# and .env is mounted in, not written.
RUN useradd --system --uid 10001 --no-create-home pokedex \
    && chown -R pokedex:pokedex /app
USER pokedex

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=2).status==200 else 1)"

# One worker on purpose: the rate limiter, lockout table and spend ledger are
# in-process. Scaling out means moving those to Redis first — see security.py.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--timeout-graceful-shutdown", "25"]
