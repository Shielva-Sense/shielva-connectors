# Mailchimp Connector — Setup Guide

This guide walks you through generating a Mailchimp API key and configuring the Shielva Mailchimp connector.

---

## 1. Log in to Mailchimp

Go to [mailchimp.com](https://mailchimp.com) and sign in to your account.

---

## 2. Generate an API Key

1. Click on your profile avatar in the lower-left corner and select **Account & billing**.
2. Navigate to **Extras → API keys**.
3. Click **Create A Key**.
4. Give the key a label (e.g. "Shielva Integration").
5. Copy the generated API key — it looks like: `a1b2c3d4e5f6789012345678901234ab-us10`

> The suffix after the final dash (e.g. `us10`) is your **data center** identifier. The connector extracts this automatically to build the correct API base URL.

---

## 3. Configure the Connector in Shielva

Enter the following in the connector install form:

| Field | Value |
|-------|-------|
| **API Key** | `your-api-key-here` (e.g. `abc123-us10`) |

That is the only required field. The connector automatically derives the data center from the key suffix.

---

## 4. What Gets Synced

| Resource | Mailchimp API | Shielva Document Type |
|----------|---------------|-----------------------|
| Audience members | `GET /lists/{id}/members` | `email_contact` |
| Campaigns | `GET /campaigns` | `email_campaign` |

The connector paginates all results using offset-based pagination and normalizes each contact into a stable document — the document ID is `SHA-256(list_id:email_address)[:16]`, enabling upsert deduplication across syncs.

---

## 5. Verify the Connection

Once installed, click **Test Connection**. The connector calls `GET /` (root endpoint) and returns your Mailchimp account name on success.

---

## API Base URL

The connector constructs the base URL from your API key's data center suffix:

```
https://{dc}.api.mailchimp.com/3.0
```

For example, with API key `abc123-us10`, the base URL is:

```
https://us10.api.mailchimp.com/3.0
```

---

## Permissions

The API key has the same permissions as your Mailchimp account user. No additional OAuth scopes or app permissions need to be configured — standard account-level API keys have full read access to your audiences, members, and campaigns.

To restrict access, consider creating a dedicated Mailchimp user with the **Viewer** role and generating an API key under that account.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| HTTP 401 | Invalid or revoked API key | Regenerate the API key in **Account → Extras → API keys** |
| HTTP 403 | Key exists but permission denied | Check that the key belongs to a user with sufficient access |
| HTTP 404 | Audience or member not found | Verify the list ID or subscriber hash is correct |
| HTTP 429 | Rate limit exceeded | Mailchimp limits vary by plan. The connector retries automatically with exponential back-off |
| Wrong data center | DC suffix missing or wrong | Ensure your API key ends with `-<dc>` (e.g. `-us10`) |
| Empty sync | No audiences or members | Confirm your Mailchimp account has at least one audience with subscribers |

### Mailchimp API Rate Limits

| Plan | Request Limit |
|------|--------------|
| Free | 10 requests/second |
| Essentials, Standard, Premium | 10 requests/second |

The connector uses offset-based pagination and retries on `429` with exponential back-off (1s, 2s). For large accounts (100k+ contacts), syncs may take several minutes.

---

## Security Notes

- API keys grant full access to your Mailchimp account. Store them securely — never commit to version control.
- Keys can be revoked at any time from **Account → Extras → API keys → Revoke**.
- Shielva stores the key encrypted at rest using AES-256-GCM via the vault.
- Consider rotating your API key periodically. After rotation, update the key in the Shielva connector config and run a health check to confirm connectivity.
