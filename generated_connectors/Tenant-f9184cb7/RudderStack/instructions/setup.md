# RudderStack Connector — Setup

This connector talks to two RudderStack surfaces with two different credentials:

| Surface       | Endpoint                            | Auth                                                |
|---------------|-------------------------------------|-----------------------------------------------------|
| Control plane | `https://api.rudderstack.com/v2/*`  | `Authorization: Bearer <management_token>`          |
| Data plane    | `https://<your-org>-data.rudder.com`| `Authorization: Basic base64(<write_key>:)`         |

## 1. Get a Management Token

1. Sign in to the RudderStack dashboard.
2. Top-right → **Profile → Settings**.
3. Open the **Personal Access Tokens** tab.
4. Click **Create Token**, give it a name (e.g. `shielva-connector`), copy the
   value — it is shown once.

Paste this value into the **Management Token** install field.

## 2. Find your Data Plane URL

It is displayed at the top of every RudderStack dashboard page (e.g.
`https://your-org-data.rudderstack.com`). Paste it into the **Data Plane URL**
install field, including the scheme.

## 3. (Optional) Default Write Key

If you set a **Default Write Key**, the connector will use it for every
`track`, `identify`, `page`, `group`, and `batch` call where the caller does
not pass an explicit `write_key`. To find it:

1. Sources tab → click your source.
2. Copy the **Write Key** shown in the source settings.

## 4. Install

The platform will call `install()` which validates the `management_token`
and `data_plane_url`, then saves the config. Once installed, `health_check()`
verifies the management token by listing one source on the control plane.

## 5. Quick smoke test

```python
status = await connector.health_check()
assert status.health.value == "healthy"

# control plane
sources = await connector.list_sources(limit=5)

# data plane (uses default_write_key)
await connector.track(user_id="u1", event="Signed Up", properties={"plan": "pro"})
```

## Notes

- The control plane is rate-limited per workspace. Default is 100 requests/min;
  override via the `rate_limit_per_min` install field.
- The data plane is rate-limited per write key — typically much higher.
- The connector retries automatically on `429` and `5xx` responses with
  exponential backoff (up to 3 retries).
- Destination creation requires a `source_id` — RudderStack binds every
  destination to exactly one source.
