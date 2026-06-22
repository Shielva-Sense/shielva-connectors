# PostHog Connector — Setup

## 1. Obtain credentials

### Personal API Key (for management endpoints)

1. Log into PostHog (US Cloud: <https://us.posthog.com>, EU: <https://eu.posthog.com>, or your self-hosted URL).
2. Click your avatar (bottom-left) → **Personal Settings** → **Personal API Keys**.
3. Click **Create personal API key**. Give it a label like "Shielva connector".
4. Grant the scopes you need:
   - `project:read` — list projects
   - `feature_flag:read` + `feature_flag:write` — manage flags
   - `cohort:read`, `insight:read`, `person:read`, `query:read` — analytics surfaces
5. Copy the `phx_…` value. **This is shown once.**

### Project API Key (for `capture_event` and `batch_capture`)

1. From the project picker (top-left), choose the project you want events to land in.
2. Go to **Project Settings** → **Project API Key** (top of the page).
3. Copy the `phc_…` value. This is the same key that posthog-js bundles ship with — it's safe to use from clients.

## 2. Install the connector

Provide both keys in the install form. The `host` field defaults to `https://us.posthog.com`; change it to `https://eu.posthog.com` (EU Cloud) or your self-hosted PostHog URL if needed. `default_project_id` is optional but convenient if all calls target one project.

## 3. Smoke test

```python
from connector import PostHogConnector

conn = PostHogConnector(
    tenant_id="tenant-1",
    connector_id="posthog-1",
    config={
        "personal_api_key": "phx_…",
        "project_api_key":  "phc_…",
        "host": "https://us.posthog.com",
        "default_project_id": 12345,
    },
)

status = await conn.health_check()             # expects health=healthy
projects = await conn.list_projects()          # expects results=[…]
await conn.capture_event("smoke-test-user", "shielva.connector.smoke")
flags = await conn.list_feature_flags(12345)
```

## 4. Endpoint reference

| Method | Endpoint | Auth |
|---|---|---|
| `health_check` | `GET /api/users/@me` | Bearer personal |
| `list_projects` | `GET /api/projects` | Bearer personal |
| `capture_event` | `POST /capture/` | project_api_key in body |
| `batch_capture` | `POST /batch/` | project_api_key in body |
| `identify` | `POST /capture/` (event=`$identify`) | project_api_key in body |
| `list_feature_flags` | `GET /api/projects/{id}/feature_flags` | Bearer personal |
| `get_feature_flag` | `GET /api/projects/{id}/feature_flags/{flag_id}` | Bearer personal |
| `create_feature_flag` | `POST /api/projects/{id}/feature_flags` | Bearer personal |
| `list_cohorts` | `GET /api/projects/{id}/cohorts` | Bearer personal |
| `list_insights` | `GET /api/projects/{id}/insights` | Bearer personal |
| `run_query` | `POST /api/projects/{id}/query` | Bearer personal |
| `list_persons` | `GET /api/projects/{id}/persons` | Bearer personal |
| `list_events` | `GET /api/projects/{id}/events` | Bearer personal |

## 5. Rate limiting

PostHog's default is 240 requests/minute per personal key against management APIs (much higher for capture). The connector retries 429 responses up to 3 times with exponential backoff, honoring `Retry-After` when present.
