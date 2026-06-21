# Clockify Connector — Setup

This connector talks to the [Clockify](https://clockify.me) REST API and the
Clockify Reports API. Auth is **API key** only — no OAuth.

## 1. Generate an API key

1. Sign in at `https://app.clockify.me`.
2. Open **Profile Settings** (top-right avatar → Profile Settings).
3. Scroll to **API** and click **Generate**.
4. Copy the generated key. Treat it like a password — it grants full
   read/write access to every workspace the user belongs to.

## 2. Install the connector

Provide the following values at install time:

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `api_key` | yes | — | The generated key. Stored as a Shielva secret. |
| `default_workspace_id` | no | — | If set, `sync()` uses this workspace. Otherwise the first workspace returned by `/workspaces` is used. |
| `base_url` | no | `https://api.clockify.me/api/v1` | Override only for self-hosted / on-prem Clockify (rare). |
| `reports_base_url` | no | `https://reports.api.clockify.me/v1` | Override only for self-hosted Reports API. |
| `rate_limit_per_min` | no | `60` | Soft limit enforced by the retry layer. |

The install step validates the key is non-empty. The first `health_check()`
call confirms the key works against `GET /user`.

## 3. Endpoints exposed

| API | Endpoint | Notes |
|-----|----------|-------|
| `health_check` | `GET /user` | Verifies the API key. |
| `get_current_user` | `GET /user` | Authenticated user profile. |
| `list_workspaces` | `GET /workspaces` | Every workspace for the user. |
| `list_projects` | `GET /workspaces/{wid}/projects` | Supports `archived`, `name`, paging. |
| `get_project` | `GET /workspaces/{wid}/projects/{pid}` | Single project. |
| `create_project` | `POST /workspaces/{wid}/projects` | `billable` + `hourlyRate` supported. |
| `list_clients` | `GET /workspaces/{wid}/clients` | Supports paging + archived. |
| `create_client` | `POST /workspaces/{wid}/clients` | |
| `list_tags` | `GET /workspaces/{wid}/tags` | Supports paging + archived. |
| `list_tasks` | `GET /workspaces/{wid}/projects/{pid}/tasks` | Default status `ACTIVE`. |
| `list_time_entries` | `GET /workspaces/{wid}/user/{uid}/time-entries` | Supports `start`, `end`, `project`. |
| `create_time_entry` | `POST /workspaces/{wid}/time-entries` | Omit `end` for a running timer. |
| `update_time_entry` | `PUT /workspaces/{wid}/time-entries/{eid}` | Generic patch body. |
| `delete_time_entry` | `DELETE /workspaces/{wid}/time-entries/{eid}` | |
| `summary_report` | `POST {reports_base}/workspaces/{wid}/reports/summary` | Different host. |

All requests carry the `X-Api-Key` header. Retries are applied on `429` and
`5xx` responses (exponential backoff + jitter).

## 4. Sync behaviour

`sync()` pulls time entries for the authenticated user from the default
workspace. Each entry is normalized via `helpers.normalizer.normalize_time_entry`
into a `NormalizedDocument` and ingested into the configured KB.

- `since` (datetime, optional) restricts to entries starting after the given
  timestamp.
- `full=True` walks pages until exhausted.

## 5. Errors

- `ClockifyAuthError` — 401/403 from the API. Re-check the API key.
- `ClockifyNotFound` — 404. Resource missing.
- `ClockifyRateLimitError` — 429. Caller retries automatically.
- `ClockifyNetworkError` — TCP/TLS failure or timeout.
- `ClockifyError` — base class; raised for any other non-2xx.

## 6. Local testing

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/clockify_connector
pip install -r requirements.txt
pytest -q
```

The test suite uses `respx` to mock every HTTP endpoint; no real Clockify
account is required.
