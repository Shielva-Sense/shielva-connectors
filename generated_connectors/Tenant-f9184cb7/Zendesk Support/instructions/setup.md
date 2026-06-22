# Zendesk Support Connector — Setup Guide

## Overview

The Zendesk connector syncs support tickets, ticket comments, and users from your Zendesk Support account into Shielva. It uses Zendesk's REST API v2 with API Token authentication.

---

## Step 1 — Enable API Token Access

1. Log in to your Zendesk Admin Center.
2. Go to **Admin → Apps & Integrations → APIs → Zendesk API**.
3. Under the **Settings** tab, ensure **Token Access** is enabled (toggle to **On**).
4. Click **Save**.

---

## Step 2 — Create an API Token

1. Still on the **Zendesk API** page, click the **API Tokens** tab.
2. Click **Add API Token**.
3. Enter a description (e.g. "Shielva Connector").
4. Click **Create**. The token is shown only once — copy it now.
5. Click **Save**.

---

## Step 3 — Gather your install fields

| Field | Where to find it | Example |
|-------|-----------------|---------|
| **Subdomain** | The prefix in your Zendesk URL: `https://<subdomain>.zendesk.com` | `mycompany` |
| **Agent Email** | Email address of the Zendesk agent who owns the token | `support@mycompany.com` |
| **API Token** | Copied in Step 2 | `GhLKj3...` |

> The agent account must have at least **Agent** role. For full ticket and user access, **Admin** role is recommended.

---

## Step 4 — Install in Shielva ACP

1. In the Shielva ACP, navigate to **Integrations → Add Connector → Zendesk Support**.
2. Fill in the three install fields: **Subdomain**, **Agent Email**, **API Token**.
3. Click **Install**. Shielva calls `GET /api/v2/users/me.json` to verify credentials.
4. On success, the connector status shows **ONLINE**.

---

## Required Permissions

The Zendesk agent used for this connector needs access to:

- **Tickets** — read access to list and retrieve tickets and comments
- **Users** — read access to list agents and end-users
- **Organizations** — read access to list organizations

Zendesk's default **Agent** role grants all of the above for tickets within the agent's groups. For cross-group ticket access, the agent needs **Admin** role or a custom role with those permissions.

---

## Scopes / Permissions Summary

| Resource | Endpoint | Required permission |
|----------|----------|---------------------|
| Current user | `GET /users/me.json` | Agent (any role) |
| Tickets | `GET /tickets.json` | Agent or Admin |
| Ticket comments | `GET /tickets/{id}/comments.json` | Agent or Admin |
| Users | `GET /users.json` | Admin or Advisor |
| Organizations | `GET /organizations.json` | Admin or Advisor |

---

## Troubleshooting

### 401 Unauthorized

**Cause:** Wrong email or API token.

**Fix:**
- Confirm the email matches the Zendesk agent account (not your personal email).
- The authentication format is `{email}/token:{api_token}` — make sure the email is the full address.
- Re-generate the token in Admin → Apps & Integrations → Zendesk API → API Tokens.

---

### 403 Forbidden

**Cause:** The agent account lacks permission to the requested resource.

**Fix:**
- Ensure the agent has Admin role, or a custom role with read access to Tickets and Users.
- For user listing, the agent must be an Admin or have the "Users and Organizations" permission.

---

### 404 Not Found

**Cause:** The subdomain is incorrect, or a specific ticket/user ID no longer exists.

**Fix:**
- Double-check the subdomain — it is the part before `.zendesk.com` in your support URL.
- Confirm the resource exists in Zendesk.

---

### 429 Too Many Requests (Rate Limit)

**Cause:** Zendesk enforces rate limits of approximately **700 requests per minute** for most plans.

**Fix:**
- The connector automatically retries with exponential backoff (up to 3 attempts).
- If 429s persist during large syncs, consider reducing sync frequency.
- Enterprise plans have higher rate limits — contact Zendesk support to increase your limit.

---

### Sync returns 0 tickets

**Cause:** No tickets match the query (e.g. incremental sync `updated_after` is in the future), or the agent can only see tickets in their own group.

**Fix:**
- Run a full sync by setting `full=True` to ignore the `updated_after` filter.
- Promote the agent account to Admin so it can see all tickets.
