# Statuspage Connector — Setup

The Shielva Statuspage connector wraps the [Atlassian Statuspage REST API](https://developer.statuspage.io/) so any Shielva agent or workflow can manage components, incidents, subscribers, and metrics for one or more Statuspage pages.

## Prerequisites

1. A Statuspage account with at least one published page.
2. An API token: **Statuspage → User Profile → API Tokens → Create API Token**.
3. (Optional) The page ID of your default Statuspage page — copy it from the URL of the Statuspage admin (`https://manage.statuspage.io/pages/<PAGE_ID>`).

## Install fields

| Field | Required | Description |
|-------|----------|-------------|
| `api_key` | yes | Statuspage API token. Sent as `Authorization: OAuth {api_key}` (note: not `Bearer`). |
| `default_page_id` | no | Shorthand fallback used when a method is called without an explicit `page_id`. |
| `base_url` | no | Defaults to `https://api.statuspage.io/v1`. |
| `rate_limit_per_min` | no | Defaults to `30` (Statuspage's per-token quota). |

## Verifying the install

After installing, the gateway calls `health_check()`, which probes `GET /pages` with the configured token. A success returns `ConnectorHealth.HEALTHY` + `AuthStatus.CONNECTED`. A `401`/`403` returns `AuthStatus.INVALID` — re-issue the token and re-install.

## Method surface

The connector exposes 16 async methods covering pages, components, incidents, subscribers, and metrics. See `metadata/connector.json` for the full API contract and parameter shapes.

## Notes

- The auth scheme keyword is `OAuth` (e.g. `Authorization: OAuth abcd-token-1234`). It is **not** `Bearer`. The HTTP client enforces this.
- Statuspage rate-limits at ~1 req/sec per token. The HTTP client retries `429` and `5xx` responses with exponential backoff + jitter, honouring `Retry-After` when present.
- Statuspage wraps all create/update bodies in a typed envelope (`{"component": {...}}`, `{"incident": {...}}`, `{"subscriber": {...}}`). The connector handles this for you.
