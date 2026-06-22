# OneLogin Connector — Setup

## 1. Create OneLogin API credentials

1. Sign in to the OneLogin admin portal at `https://<your-subdomain>.onelogin.com`.
2. Navigate to **Developers → API Credentials**.
3. Click **New Credential** and grant the scope you need:
   - **Manage Users** – required for `list_users`, `get_user`, `create_user`,
     `update_user`, `delete_user`, `set_user_state`, and `assign_role_to_user`.
   - **Manage All** – also required for `list_apps`, `get_app`,
     `assign_app_to_user`, `list_roles`, `list_events`, and
     `create_session_login_token`.
4. Save the generated **Client ID** and **Client Secret** — the secret is shown
   only once.

## 2. Install the connector in Shielva

Fill the install form with:

| Field             | Value                                                          |
|-------------------|----------------------------------------------------------------|
| Subdomain         | The first label of your OneLogin URL — e.g. `acme` from `acme.onelogin.com` |
| API Client ID     | The Client ID from step 1                                       |
| API Client Secret | The Client Secret from step 1                                   |
| Region            | `us` (default) or your data residency region                    |
| Rate Limit / min  | `60` (default; raise only if your OneLogin plan supports it)    |

The base URL is computed automatically as
`https://{subdomain}.onelogin.com/api/2`.

## 3. Authenticate

After installation, the gateway calls `authenticate()` which posts
`grant_type=client_credentials` to `{base_url}/oauth2/token` using
HTTP-Basic-encoded `client_id:client_secret`. The resulting access token is
cached in-process until 60s before its `expires_at`, after which the connector
will silently refresh on the next request.

If a request returns 401 the connector clears the cached token, re-runs the
client-credentials flow, and retries the original request exactly once. 429
and 5xx responses are retried once with `Retry-After` honoured when present.

## 4. Health check

`health_check()` lists one user (`GET /users?limit=1`) — a 200 response means
the connector is healthy and the cached access token is accepted by OneLogin.

## 5. Session login token (interactive sign-in)

`create_session_login_token(username, password)` calls `POST /login/auth`
with the end-user's credentials and returns a OneLogin session token. The
Bearer access token from step 3 is still required; only the *body* carries the
end-user credentials.

## 6. Audit + events

`list_events(limit, since, event_type_id)` reads the OneLogin audit log. Pass
`since="2026-06-21T00:00:00Z"` for incremental pulls. Each event can be
normalized with `OneLoginConnector.get_event_as_document(event)` to ingest into
the Shielva knowledge base.
