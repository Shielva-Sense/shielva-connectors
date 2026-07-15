# syntax=docker/dockerfile:1.7-labs
# shielva-connectors — FastAPI service, multi-stage build (self-context).
# shielva-common installs from the private Shielva-AI/shielva-platform-core repo
# via a BuildKit secret `github_token` (.netrc written→used→deleted in one RUN;
# token never enters a layer).
#   docker build --secret id=github_token,env=GITHUB_TOKEN -t shielva-connectors:dev .

FROM python:3.12-slim AS builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libpq-dev git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN --mount=type=secret,id=github_token \
    printf 'machine github.com\n  login x-access-token\n  password %s\n' \
      "$(cat /run/secrets/github_token)" > /root/.netrc \
    && chmod 600 /root/.netrc \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && rm -f /root/.netrc

# Connector ARTIFACTS are NO LONGER baked into the image. Each connector's wheel
# is pip-installed from the JFrog PyPI repo ON DEMAND at runtime — the first time a
# tenant installs/uses it (gateway._ensure_connector_installed) and re-hydrated on
# boot for already-installed connectors. This keeps the image small (only the SDK
# dependency closure for connectors actually IN USE lands on the pod's disk).
# Runtime needs JFrog read creds: env JFROG_TOKEN / JFROG_USER (+ optional
# JFROG_INDEX_HOST / JFROG_REPO). The connectors-requirements.txt manifest is still
# shipped (via the runtime COPY . . below) — the gateway reads it to pin versions.

FROM python:3.12-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 libssl3 curl \
    && rm -rf /var/lib/apt/lists/*
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --chown=appuser:appgroup . .
# ── merge the connector-SDK `shared/*` into the ONE `shared` package ──────────
# `shared` is a SINGLE top-level package split across two sources: the connector
# SDK ships base_connector/oauth_handler/repository_service here in the repo, while
# shielva-common ships discovery_client/correlation/__init__ via pip (into
# site-packages/shared). Locally server.sh symlinks shielva-common's files INTO
# core/shared/; in the image we fold the SDK modules INTO the pip-installed
# `shared` package so a single top-level `shared` exposes BOTH. Without this the
# two dirs (/app/shared without __init__, site-packages/shared regular pkg) shadow
# each other and on-demand connector wheels (`from shared.base_connector import …`)
# fail to import → no connector loads → /connectors/{type}/docs 404s.
RUN cp /app/shared/base_connector.py /app/shared/oauth_handler.py /app/shared/repository_service.py \
       /usr/local/lib/python3.12/site-packages/shared/
# On-demand connector wheels install to a writable USER-site dir (base
# site-packages stays root-owned/immutable). Python auto-adds the user site to
# sys.path for the non-root user, so entry_points() discovers the new wheels.
ENV PYTHONUSERBASE=/opt/conn
# Pre-create the user-site dir so site.py adds it to sys.path at startup (then
# on-demand-installed wheels are importable without a re-exec).
RUN mkdir -p /opt/conn/lib/python3.12/site-packages && chown -R appuser:appgroup /opt/conn
# integration.main writes a relative `logs/` dir (LOG_DIR = Path("logs")) at
# import time; /app is root-owned after COPY, so a fresh build 500s on boot
# ("Permission denied: 'logs'") the moment it runs as appuser. Pre-create the
# dir and make /app writable by appuser so relative writes (logs, tmp) succeed.
RUN mkdir -p /app/logs && chown appuser:appgroup /app /app/logs
USER appuser
EXPOSE 8003
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8003/health || exit 1
CMD ["uvicorn", "integration.main:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "2"]
