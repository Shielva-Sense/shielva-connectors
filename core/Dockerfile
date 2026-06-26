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

# Connector ARTIFACTS — install the connector library (one wheel per connector)
# from the JFrog PyPI repo, pinned by the committed manifest. The gateway then
# discovers them at runtime via the `shielva.connectors` entry-points
# (_load_installed_connectors). Token is a BuildKit secret (.netrc written→used→
# deleted in one layer, never persisted). Skipped when no secret/manifest is
# supplied, so local/dev images build without JFrog.
#   docker build --secret id=jfrog_token,env=JFROG_TOKEN \
#                --build-arg JFROG_USER=<you> -t shielva-connectors:dev .
ARG JFROG_INDEX_HOST=trialrifms4.jfrog.io
ARG JFROG_REPO=shielva592-42
ARG JFROG_USER=""
COPY connectors-requirements.txt ./connectors-requirements.txt
RUN --mount=type=secret,id=jfrog_token \
    if [ -s /run/secrets/jfrog_token ] && [ -f connectors-requirements.txt ]; then \
      printf 'machine %s\n  login %s\n  password %s\n' \
        "${JFROG_INDEX_HOST}" "${JFROG_USER}" "$(cat /run/secrets/jfrog_token)" > /root/.netrc \
      && chmod 600 /root/.netrc \
      && pip install --no-cache-dir \
           --index-url "https://${JFROG_INDEX_HOST}/artifactory/api/pypi/${JFROG_REPO}/simple" \
           --extra-index-url https://pypi.org/simple/ \
           -r connectors-requirements.txt \
      && rm -f /root/.netrc ; \
    else echo "no jfrog secret/manifest — skipping connector artifact install"; fi

FROM python:3.12-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 libssl3 curl \
    && rm -rf /var/lib/apt/lists/*
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . .
RUN chown -R appuser:appgroup /app
USER appuser
EXPOSE 8003
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8003/health || exit 1
CMD ["uvicorn", "integration.main:app", "--host", "0.0.0.0", "--port", "8003", "--workers", "2"]
