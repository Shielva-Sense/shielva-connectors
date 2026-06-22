# Drata Connector — Setup

The Drata connector wraps the [Drata public API](https://developers.drata.com/)
and exposes compliance resources (frameworks, controls, policies, tests,
evidence, personnel, vendors, user-access reviews) to Shielva.

## 1. Generate a Drata API key

1. Sign in to the Drata web UI as an admin.
2. Navigate to **Settings → API Keys**.
3. Click **New API Key**, give it a name (e.g. `shielva-connector`), pick the
   scopes you need (read for all listed APIs; write for `create_vendor` and
   `update_personnel`), and **Generate**.
4. Copy the key immediately — Drata only shows it once.

## 2. Install the connector in Shielva

Open the Shielva App Store, pick **Drata**, and fill in:

| Field | Required | Notes |
|---|---|---|
| **API Key** | yes | Paste the key from step 1. Stored encrypted at rest. |
| **Default Workspace ID** | no | If you mostly operate on a single workspace, paste its `ws_*` id here. |
| **Drata API Base URL** | no | Leave blank to use `https://public-api.drata.com`. |
| **API Version** | no | Defaults to `2024-09` (sent as `Drata-Api-Version`). |
| **Rate Limit (requests/min)** | no | Defaults to `60`. |

Click **Install**. The connector will move to `PENDING`.

## 3. Verify connectivity

Run **Health Check** (or call `health_check()` from your code). The connector
calls `GET /workspaces` against the configured base URL. On success the status
moves to `CONNECTED`. On 401 the status moves to `TOKEN_EXPIRED` — re-issue
the API key in Drata and update the connector config.

## 4. Available methods

All methods are async and return raw JSON dicts unless otherwise noted.

- `list_workspaces()`
- `list_personnel(workspace_id, status=None, limit=100, offset=0)`
- `get_personnel(workspace_id, personnel_id)`
- `update_personnel(workspace_id, personnel_id, fields: dict)`
- `list_vendors(workspace_id, status=None, limit=100, offset=0)`
- `create_vendor(workspace_id, name, website_url=None, description=None, criticality=None)`
- `list_controls(workspace_id, framework=None, limit=100, offset=0)`
- `get_control(workspace_id, control_id)`
- `list_policies(workspace_id, limit=100, offset=0)`
- `get_policy(workspace_id, policy_id)`
- `list_tests(workspace_id, status=None, limit=100)`
- `list_frameworks(workspace_id)`
- `list_user_access_reviews(workspace_id, status=None)`
- `list_evidence(workspace_id, control_id=None, limit=100)`

`DrataConnector.iter_items(payload)` yields the rows from a list response
regardless of whether Drata returns a bare list, `{"data": [...]}`, or
`{"items": [...]}`.

## 5. Error handling

| Exception | Cause |
|---|---|
| `DrataAuthError` | 401 / 403 — API key invalid, revoked, or missing scopes. |
| `DrataNotFound` | 404 — workspace/personnel/control id does not exist. |
| `DrataRateLimitError` | 429 after retry budget is exhausted. |
| `DrataNetworkError` | Transport-level failure (timeout, DNS, connection reset). |
| `DrataError` | Any other Drata API error. |

The HTTP client retries 429 and 5xx responses up to 3 times with exponential
backoff (honoring `Retry-After` when present).
