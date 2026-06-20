# Wrike Connector — Setup Guide

Connect Shielva to Wrike to sync your folders, tasks, users, comments, and timelogs into your knowledge base for AI-powered search and workflows.

---

## Prerequisites

- A Wrike account with **Admin** or **Owner** role.
- Access to **Wrike Apps & Integrations** settings.

---

## Step 1 — Create a Wrike OAuth App

1. Log in to Wrike at [wrike.com](https://www.wrike.com).
2. Click your **avatar** (top-right) → **Apps & Integrations**.
3. In the left sidebar, click **API**.
4. Click **Create new app** (or **Register your app**).
5. Fill in the required fields:
   - **App name**: `Shielva Integration` (or any descriptive name)
   - **App description**: optional
   - **Redirect URIs**: enter your Shielva OAuth callback URL, e.g.
     `https://app.shielva.ai/oauth/callback/wrike`
6. Click **Save**.
7. Wrike will display your **Client ID** and **Client Secret**.
   Copy both values immediately — the Client Secret is only shown once.

---

## Step 2 — Install the Connector in Shielva

1. In Shielva, navigate to **Integrations → Wrike**.
2. Enter the following fields:
   - **Client ID** — paste from Step 1
   - **Client Secret** — paste from Step 1
   - **Redirect URI** *(optional)* — the same redirect URI you registered in Step 1;
     leave blank to use the Shielva default
3. Click **Install**.

---

## Step 3 — Authorize (OAuth Flow)

After installation, the connector is in `PENDING_OAUTH` state. You must complete the OAuth flow:

1. The Shielva integration builder will display an **Authorize** button.
2. Click it. Shielva calls `connector.authorize()` which builds the authorization URL:
   ```
   https://login.wrike.com/oauth2/authorize/v4
     ?client_id=YOUR_CLIENT_ID
     &response_type=code
     &scope=Default
     &redirect_uri=YOUR_REDIRECT_URI
   ```
3. Your browser opens the Wrike consent page.
4. Review the requested permissions and click **Accept**.
5. Wrike redirects back to the redirect URI with a `code` parameter.
6. Shielva exchanges the `code` for an `access_token` and `refresh_token` via:
   ```
   POST https://login.wrike.com/oauth2/token
   ```
7. The connector is now connected. Status changes to `CONNECTED`.

---

## Step 4 — Scopes

Wrike uses a **Default** scope that grants read and write access to all resources
the user can access. For read-only sync you can request narrower scopes:

| Scope                  | Description                          |
|------------------------|--------------------------------------|
| `Default`              | Full access (recommended for sync)   |
| `amReadOnlyWorkflow`   | Read-only access to workflows        |
| `amReadOnlyUser`       | Read-only access to users/contacts   |
| `amReadOnlyTask`       | Read-only access to tasks            |

To use narrower scopes, add a `scope` key to your connector config before authorizing:
```json
{"scope": "amReadOnlyTask amReadOnlyUser amReadOnlyWorkflow"}
```

---

## Step 5 — Run a Sync

Once connected, trigger a sync from the Shielva integration dashboard, or programmatically:

```python
from connector import WrikeConnector

async with WrikeConnector(config={
    "client_id": "YOUR_CLIENT_ID",
    "client_secret": "YOUR_CLIENT_SECRET",
    "access_token": "ACCESS_TOKEN_FROM_OAUTH",
    "refresh_token": "REFRESH_TOKEN_FROM_OAUTH",
}) as conn:
    result = await conn.sync(full=True, kb_id="kb_wrike_001")
    print(f"Synced {result.documents_synced} documents ({result.documents_found} found)")
```

---

## Token Refresh

Wrike access tokens expire. The connector automatically refreshes them using the
stored `refresh_token` when a `401 Unauthorized` response is received mid-request.
No manual intervention is required.

To manually trigger a refresh:
```python
refreshed = await conn.client.refresh_access_token()
# Store refreshed["access_token"] and refreshed["refresh_token"] securely.
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `auth_status: invalid_credentials` | Access token expired or revoked | Re-authorize: click **Authorize** in Shielva integrations |
| `auth_status: missing_credentials` | client_id / client_secret not entered | Re-enter both fields in the Install step |
| `health: degraded` after successful auth | Wrike API temporarily unreachable | Retry after a few minutes; the connector retries 3× with backoff |
| `403 Forbidden` on specific resources | Wrike account doesn't have access to the resource | Verify the connected user has access in Wrike |

---

## Data Synced

| Resource | Wrike API Endpoint | Normalization |
|----------|-------------------|---------------|
| Folders & Projects | `GET /folders` | Title, description, color, scope, project status |
| Tasks | `GET /tasks` (paginated via `nextPageToken`) | Title, description, status, importance, due date, assignees |
| Users / Contacts | `GET /contacts` | First/last name, email, role, active status |
| Comments | `GET /comments` (paginated via `nextPageToken`) | Author, text, created date, linked task/folder |
| Timelogs | `GET /timelogs` | Synced as-is for reference |

All documents are normalized to Shielva's `ConnectorDocument` schema with stable
`source_id` values (sha256-prefixed, namespaced by resource type) for deduplication.
