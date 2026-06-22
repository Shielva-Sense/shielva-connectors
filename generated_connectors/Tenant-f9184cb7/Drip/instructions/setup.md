# Drip Connector — Setup

The Drip connector talks to the [Drip v2 REST API](https://developer.drip.com/)
using HTTP Basic authentication: the **API token** is the username, the
password is empty.

## 1. Find your Drip Account ID

1. Sign in to <https://www.getdrip.com/>.
2. Open **Settings → General Info**.
3. Copy the numeric **Account ID** (it appears in the URL too, e.g.
   `app.getdrip.com/1234567/…`).

## 2. Generate an API Token

1. From the avatar menu choose **User Settings**.
2. Open the **My User Settings** tab.
3. Scroll to the **API Token** section and copy the token (or generate a new
   one). Treat it as a password — anyone with the token can read and modify
   every subscriber and campaign in the account.

## 3. Install the connector in Shielva

1. In the Shielva platform, open **Connectors → Add Connector → Drip**.
2. Paste the values from steps 1–2:
   - **Drip Account ID**: e.g. `1234567`
   - **Drip API Token**: the token from step 2
   - **Base URL** (optional): defaults to `https://api.getdrip.com/v2`
   - **Rate Limit**: defaults to `3600` requests/minute (Drip's standard cap)
3. Click **Install**. The connector immediately calls `GET /campaigns` to
   verify the credentials. A `401 Unauthorized` here means the token is wrong
   or revoked — the install will fail without writing anything.

## 4. What you can do once installed

| API | Purpose |
| --- | --- |
| `list_subscribers` / `get_subscriber` | Read the audience |
| `create_or_update_subscriber` | Sync customer records into Drip |
| `tag_subscriber` / `untag_subscriber` | Drive segmentation |
| `record_event` | Trigger Drip automations from product events |
| `list_campaigns` / `subscribe_to_campaign` | Manage drip campaigns |
| `list_workflows` / `start_workflow` | Manage visual workflows |
| `list_broadcasts` | Read broadcast emails |

## 5. Rotating the API Token

To rotate: generate a new token in Drip, update the connector's `api_token`
field through the platform, and re-run **Health Check**. The old token can be
deleted from Drip after a successful health check.

## 6. Troubleshooting

- **401 Unauthorized** — the token is wrong, revoked, or you are using the
  wrong Account ID. Confirm both values and re-install.
- **429 Rate limit** — the connector retries automatically (exponential
  backoff, up to 3 retries). If you see sustained 429s, lower
  `rate_limit_per_min` in the connector config.
- **404 on `/subscribers/{email}`** — the email is not in Drip yet. Use
  `create_or_update_subscriber` first.
