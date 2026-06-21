# Microsoft OneNote — Setup Instructions

This connector talks to the Microsoft Graph **OneNote** API on behalf of a signed-in Microsoft 365 user. Authentication is **OAuth 2.0 Authorization Code** flow via the Microsoft identity platform.

## 1. Register an Azure AD application

1. Sign in to the [Azure Portal](https://portal.azure.com/) → **Azure Active Directory** → **App registrations** → **New registration**.
2. **Name** — e.g. `Shielva OneNote Connector`.
3. **Supported account types** — choose one:
   - *Accounts in any organizational directory and personal Microsoft accounts* — keep `tenant_id = common`.
   - *Single tenant only* — set `tenant_id` to your Azure AD tenant GUID or verified domain.
4. **Redirect URI** — *Web*, value = your Shielva gateway redirect URI (e.g. `https://gateway.shielva.local/oauth/callback`).
5. Click **Register**.

## 2. Add API permissions

App → **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions** → check:

- `Notes.Read`
- `Notes.ReadWrite`
- `offline_access`
- `User.Read` (added by default — leave it)

Click **Add permissions**. If your tenant requires admin consent, click **Grant admin consent**.

## 3. Generate a client secret

App → **Certificates & secrets** → **New client secret**. Pick an expiry, click **Add**, then **copy the secret VALUE** (not the ID) immediately — the portal will not show it again.

## 4. Install the connector in Shielva

In the Shielva connector catalogue, install **Microsoft OneNote** and provide:

| Field            | Value                                                                              |
|------------------|------------------------------------------------------------------------------------|
| `client_id`      | Application (client) ID from the app **Overview** page.                            |
| `client_secret`  | Secret VALUE from step 3.                                                          |
| `tenant_id`      | `common` for multi-tenant, or your tenant GUID/domain for single-tenant.           |
| `scopes`         | Leave default `Notes.ReadWrite Notes.Read offline_access`.                         |
| `auth_url`       | Leave default.                                                                     |
| `token_url`      | Leave default.                                                                     |
| `base_url`       | Leave default.                                                                     |
| `rate_limit_per_min` | Leave default `120`.                                                           |

Click **Install** — the connector will be marked `PENDING` until OAuth is complete.

## 5. Complete the OAuth flow

Click **Connect** in the Shielva UI. You will be redirected to Microsoft's consent screen — sign in with the Microsoft 365 user whose OneNote you want to access, then approve the requested permissions. On success the connector status becomes `CONNECTED` and the platform stores the refresh token (encrypted at rest).

## 6. Verify

Click **Health Check** in the Shielva UI — the connector lists one notebook to confirm the token works. You can then call any of the APIs:

- `list_notebooks(top, skip, filter, orderby)`
- `get_notebook(notebook_id)`
- `create_notebook(display_name)`
- `list_sections(notebook_id?, top, skip)`
- `get_section(section_id)`
- `create_section(notebook_id, display_name)`
- `list_section_groups(notebook_id?)`
- `list_pages(section_id?, top, skip, filter, search)`
- `get_page(page_id)`
- `get_page_content(page_id)` — returns XHTML
- `create_page(section_id, html_body, content_type='application/xhtml+xml')`
- `update_page(page_id, commands)`
- `delete_page(page_id)`
- `copy_page_to_section(page_id, target_section_id, group_id?)`

## Notes on page bodies

OneNote pages are stored as **XHTML**, not JSON. To create a page, pass a fully-formed XHTML document as `html_body`. The `helpers.utils.build_simple_page_xhtml(title, body_html)` helper builds a minimal valid envelope:

```html
<!DOCTYPE html>
<html>
  <head><title>{title}</title></head>
  <body>{body_html}</body>
</html>
```

For multipart bodies with embedded images, send `content_type="multipart/form-data; boundary=..."` and craft the payload accordingly.

## Throttling

The Graph API uses `Retry-After` headers for HTTP 429 responses. The HTTP client honors this header up to 3 retries with exponential backoff; longer-running sync jobs additionally wrap each call in `with_retry()` from `helpers/utils.py`.
