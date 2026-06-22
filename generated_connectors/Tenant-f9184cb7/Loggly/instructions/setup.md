# Loggly Connector — Setup

## 1. Find your Loggly subdomain

Sign in to Loggly. Your account URL looks like `https://mycompany.loggly.com`.
Enter only the subdomain part (`mycompany`) in the install field.

## 2. Get login credentials

The connector authenticates to the Management API with HTTP Basic auth using
your Loggly login email and password. A dedicated read-only Loggly user is
recommended.

## 3. Get a customer token (for ingestion)

Source Setup → **HTTP/S Endpoint** → copy the customer token. Paste it into the
**Customer Token** install field.

You can also pass the token at call time to `submit_log()` / `bulk_submit_logs()`
if you want to ingest into multiple Loggly sources from one connector.

## 4. (Optional) Customize the ingestion base URL

Default is `https://logs-01.loggly.com` (US region). EU users may use
`https://logs-01.loggly.com` too — Loggly routes regionally.

## 5. Click Install

The connector will validate that subdomain + username + password are present and
mark itself `AUTHENTICATED`. Health-check will call `GET /search?q=*&size=1`
under the Management API to confirm Basic-auth credentials work.

## API surface

| Method | Endpoint |
|---|---|
| `health_check()` | `GET /search?q=*&size=1` |
| `submit_log(token, msg, tags)` | `POST {ingest}/inputs/{token}/tag/{tags}/` |
| `bulk_submit_logs(token, lines, tags)` | `POST {ingest}/bulk/{token}/tag/{tags}/` |
| `search(q, from, until, order, size)` | `GET /search` |
| `get_events(rsid, page, columns, format)` | `GET /events?rsid=...` |
| `terms_iteration(rsid, field)` | `GET /fields/{field}/?rsid=...` |
| `list_saved_searches()` | `GET /search/saved` |
| `create_saved_search(name, query, description)` | `POST /search/saved` |
| `delete_saved_search(id)` | `DELETE /search/saved/{id}` |
| `list_alerts()` | `GET /alerts` |
| `create_alert(...)` | `POST /alerts` |
| `update_alert(id, fields)` | `PUT /alerts/{id}` |
| `delete_alert(id)` | `DELETE /alerts/{id}` |
| `list_dashboards()` | `GET /dashboards` |
| `list_notification_endpoints()` | `GET /alerts/endpoints` |
