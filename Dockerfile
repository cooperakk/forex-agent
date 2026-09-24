# ---------------------------------------------------------------------------
# Stage 1 -- build the dashboard.
# Node is only needed here; it never reaches the runtime image.
# ---------------------------------------------------------------------------
FROM node:22-alpine AS dashboard

WORKDIR /build
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY dashboard/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 -- python dependencies, compiled once into a wheel cache.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS deps

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 3 -- runtime. No compiler, no node, no package index, non-root.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SENTINEL_CONFIG=/data/config.json

# tini reaps zombies and, more importantly, forwards SIGTERM so the agent gets
# its shutdown path instead of being killed mid-order.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin sentinel

WORKDIR /app

COPY --from=deps /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

COPY sentinel/ ./sentinel/
COPY scripts/ ./scripts/
COPY pyproject.toml pytest.ini ./
COPY --from=dashboard /build/dist ./dashboard/dist

# /data holds the audit log, the verdict registry, agent state and the
# configuration. It is the only writable path and it must be a volume: losing
# it loses the drawdown ladder's memory and the acceptance history.
RUN mkdir -p /data && chown -R sentinel:sentinel /app /data
VOLUME ["/data"]

USER sentinel
EXPOSE 8088

# LIVENESS, not readiness. /api/health requires authentication, so an
# unauthenticated probe gets 401 -- and 401 is a perfectly good proof that the
# process is serving, while leaking nothing. Anything below 500 counts as
# alive; only a 5xx or a refused connection is a failure.
#
# This probe deliberately cannot detect a hung decision loop: the API can
# answer while the loop is wedged. That is the watchdog's job, and it watches
# the heartbeat file rather than an HTTP endpoint for exactly this reason.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import httpx,sys; r=httpx.get('http://127.0.0.1:8088/api/health', timeout=4); sys.exit(0 if r.status_code < 500 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
# --host is passed here so the container can be reached through its port
# mapping, and serve.py now hands the SAME host to create_app -- so the
# public-bind refusal and the dashboard's permanent exposure banner both
# evaluate against what is actually bound, not against the config file.
CMD ["python", "scripts/serve.py", "--config", "/data/config.json", \
     "--host", "0.0.0.0", "--port", "8088", "--dashboard", "dashboard/dist"]
