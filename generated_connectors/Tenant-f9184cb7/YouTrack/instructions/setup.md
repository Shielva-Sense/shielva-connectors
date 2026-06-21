# YouTrack Connector — Setup

The Shielva **YouTrack** connector talks to the JetBrains YouTrack REST API
using a per-user **permanent token**. No OAuth flow is required.

## 1. Generate a permanent token

1. Sign in to your YouTrack instance as the user the connector should act as.
2. Click your avatar → **Profile** → **Account Security**.
3. Under **Permanent Tokens**, click **New token…**.
4. Give it a scope name (e.g. `Shielva Connector`) and pick the YouTrack apps
   the token may access — at minimum **YouTrack**.
5. Click **Create**. Copy the value that starts with `perm:` — you will not be
   able to view it again.

> Tokens belong to a user and inherit that user's permissions. Use a dedicated
> service-account user where possible.

## 2. Install the connector

Provide the following install fields:

| Field | Required | Example | Notes |
|---|---|---|---|
| `instance_url` | yes | `https://yourorg.youtrack.cloud` | Base instance URL. The connector appends `/api` automatically. Self-hosted URLs work too (e.g. `https://youtrack.example.com`). |
| `permanent_token` | yes | `perm:dXNlcg==.NDU=.…` | The token copied in step 1. Stored as a secret. |
| `default_project_id` | no | `0-1` | If set, `sync()` only ingests issues from this project and `create_issue` will use it when the caller omits one. |
| `rate_limit_per_min` | no | `200` | Soft client-side cap. |

The install path calls `GET /users/me?fields=login` to verify the token. If
that probe returns 401 the install fails with `INVALID_CREDENTIALS`.

## 3. Quick check

After install the connector exposes:

- `health_check()` — re-runs the `/users/me` probe.
- `list_projects()` — lists all projects visible to the token.
- `list_issues(query="project: ACME #Unresolved")` — full YouTrack query language.
- `create_issue(project_id, summary, description)` — creates an issue.
- `apply_command(issues=["2-15"], query="State Fixed", comment="Done")` — bulk command.

See `metadata/connector.json` for the full API surface.

## 4. Common errors

- **401 Unauthorized** — the permanent token has been revoked or the user is
  banned. Generate a new token.
- **404 Not Found** on a project or issue — the token's user cannot see that
  resource. Check project visibility.
- **429 Too Many Requests** — the connector automatically retries with
  exponential backoff (up to 3 attempts).

## 5. YouTrack query language

The connector passes the `query` parameter through unchanged. Useful snippets:

- `project: ACME` — restrict to a project
- `#Unresolved` — only open issues
- `updated: today` / `created: 2026-01-01 .. 2026-06-01`
- `for: me` — issues assigned to the token's user
- `tag: critical`

Full reference: <https://www.jetbrains.com/help/youtrack/cloud/search-and-command-attributes.html>
