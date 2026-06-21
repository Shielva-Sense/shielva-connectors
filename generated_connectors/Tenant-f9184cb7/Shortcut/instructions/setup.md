# Shortcut connector — setup

## 1. Generate a Shortcut API token

1. Sign in to https://app.shortcut.com
2. Click your avatar (bottom-left) → **Settings**
3. **API Tokens** in the left nav
4. **Generate Token**, label it `shielva-connector`, copy the token — Shortcut shows it only once.

Tokens inherit the issuing user's workspace permissions, so create the token from
an account that has the access the connector should have (read-only vs.
read-write).

## 2. Install the connector

In the Shielva console, install **Shortcut** and supply:

| Field | Value |
|-------|-------|
| `api_token` | the token you just copied (required, stored encrypted) |
| `base_url` | leave default (`https://api.app.shortcut.com/api/v3`) unless on a custom region |
| `default_workflow_state_id` | optional — used as fallback in `create_story()` |
| `rate_limit_per_min` | default `200` (Shortcut's documented limit) |

The connector probes `GET /member` during install; a 401 here means the token
is wrong or revoked.

## 3. Discover IDs

Most write endpoints take numeric IDs.  Use:

* `list_workflows()` → `workflow_state_id`
* `list_projects()` → `project_id`
* `list_teams()` → `group_id` (Shortcut calls these "teams" in the UI but
  "groups" in some API responses)
* `list_members()` → `owner_ids`
* `list_epics()` → `epic_id`

## 4. Searching stories

The search DSL is documented at
https://help.shortcut.com/hc/en-us/articles/360000046646-Searching-in-Shortcut.

Common examples:

```text
state:"In Progress" owner:alice
type:bug -is:archived
epic:123 updated:>2026-01-01
```

Pass the query to `list_stories_search(query=...)` and use the returned
`next` token for pagination.
