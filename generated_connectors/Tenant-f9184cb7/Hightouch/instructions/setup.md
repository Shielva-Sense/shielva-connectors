# Hightouch Connector — Setup

## 1. Generate a Workspace API Key

1. Sign in to https://app.hightouch.com.
2. Open **Settings → Personal Access Tokens**.
3. Click **Create token**, scope it to the workspace you want Shielva to manage, and copy the key (it is shown once).
4. The token must be a **Workspace API key** for org-wide access — user PATs work but are tied to a single user.

## 2. Install the connector in Shielva

| Field | Value |
| --- | --- |
| `api_key` | The token from step 1. Stored as a secret. |
| `workspace_slug` | (Optional) Default workspace slug for shorthand operations. |
| `base_url` | Default `https://api.hightouch.com/api/v1`. Override only for staging / EU residency. |
| `rate_limit_per_min` | Default `60`. Lower if you share the token with other automation. |

On install, the connector calls `GET /workspaces` to verify the key. A 401 returns `AuthStatus.INVALID_CREDENTIALS`; a network error returns `AuthStatus.PENDING` with the connector still installed.

## 3. Available operations

- `health_check()` — pings `/workspaces`
- `list_workspaces()`
- `list_sources(page, per_page, slug)` / `get_source(source_id)`
- `list_destinations(page, per_page, slug)` / `get_destination(destination_id)`
- `list_syncs(page, per_page, slug, model_id, destination_id)` / `get_sync(sync_id)`
- `trigger_sync(sync_id, full_resync=False)` — POSTs `{"fullResync": ...}` to `/syncs/{id}/trigger`
- `list_sync_runs(sync_id, page, per_page)` / `get_sync_run(sync_id, run_id)`
- `list_models(page, per_page, slug)` / `get_model(model_id)`
- `query_model(model_id, primary_key_values, limit, offset)` — POSTs to `/models/{id}/preview`

## 4. Rate limits and retries

The HTTP client retries automatically on `429 Too Many Requests` and `5xx`. It honors the `Retry-After` header when present, otherwise uses an exponential backoff (`0.5s × 2^attempt`) for up to 3 retries. Persistent failures bubble up as `HightouchError` (`status_code`, `response_body` populated).

## 5. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `401 Unauthorized` | Revoked / typo'd key | Regenerate the workspace API key and re-install. |
| `404 Not Found` on `get_sync` | Wrong workspace scope | Confirm the token belongs to the workspace that owns the sync. |
| Repeated `429` with no recovery | Workspace burst limit hit | Lower `rate_limit_per_min`, stagger triggers. |
| `HightouchNetworkError` | DNS / TLS / outbound firewall | Whitelist `api.hightouch.com` egress. |

## 6. Security

- The API key is stored as a `TokenInfo.access_token` and sealed via the platform credential store. It never appears in logs.
- All requests use TLS to `api.hightouch.com`.
- The connector is multi-tenant — every operation is scoped by the connector's `tenant_id` and `connector_id`.
