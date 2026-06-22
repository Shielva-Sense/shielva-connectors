# Iterable Connector — Setup

## 1. Generate a Server-Side API Key

1. Sign in to [Iterable](https://app.iterable.com/) (or `https://app.eu.iterable.com/` for EU).
2. Open **Integrations → API Keys**.
3. Click **New API Key**.
4. Choose **Server-Side** as the key type. (Mobile / JWT keys will be rejected by this connector.)
5. Give it a descriptive name (e.g. `shielva-iterable-prod`).
6. Copy the key value — Iterable only shows it once.

## 2. Install the Connector

In the Shielva connector marketplace, choose **Iterable** and supply:

| Field | Required | Example |
|-------|----------|---------|
| `api_key` | yes | the value you just copied |
| `region` | no  | `us` (default) or `eu` |
| `base_url` | no | leave blank unless Iterable assigned you a custom endpoint |
| `rate_limit_per_min` | no | `100` (default) |

The connector calls `GET /lists` during the health check to verify the key.

## 3. Verify

After install, hit the **Health Check** action in the Shielva connector console.
A healthy connector returns `auth_status=connected`.

## 4. Day-2 operations

- **Rotate the key**: regenerate in Iterable, update the connector secret in
  Shielva, re-run the health check. Old keys can be revoked in the Iterable UI.
- **Switch region**: change the `region` field (or set an explicit `base_url`)
  and re-run the health check. The connector picks the new base URL on the
  next install / restart.

## 5. Required Iterable permissions

The server-side API key needs at minimum:

- Users: Read, Write, Update
- Events: Track
- Lists: Read, Write, Subscribe, Unsubscribe
- Templates: Read
- Campaigns: Trigger (for `/email/target`)
