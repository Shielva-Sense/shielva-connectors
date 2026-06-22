# LinkedIn Connector — Setup Guide

## Overview

The LinkedIn connector syncs your LinkedIn profile, member posts, and company pages into the Shielva knowledge base using the LinkedIn REST API v2 with OAuth 2.0 authentication. It supports profile data, recent shares/posts (via the Shares API), and organization pages.

---

## Prerequisites

- A **LinkedIn account** with content you want to sync.
- A **LinkedIn Developer App** with the required API products approved (see Step 1).

---

## Step 1 — Create a LinkedIn App

1. Go to the [LinkedIn Developer Portal](https://developer.linkedin.com/) and sign in.
2. Click **My apps** → **Create app**.
3. Fill in:
   - **App name**: e.g., "Shielva Connector"
   - **LinkedIn Page**: associate with your company page (required for API access)
   - **App logo**: upload any logo
4. Click **Create app**.

---

## Step 2 — Request API Products

In your app settings, go to the **Products** tab and request access to:

| Product | Required Scopes |
|---|---|
| **Sign In with LinkedIn using OpenID Connect** | `openid`, `profile`, `email` |
| **Share on LinkedIn** | `w_member_social` |
| **Marketing Developer Platform** | `r_organization_social` (for company pages) |

Some products require review and approval from LinkedIn. Basic profile + share access is typically approved instantly.

---

## Step 3 — Configure OAuth Scopes

In your app settings, go to **Auth** → **OAuth 2.0 scopes** and verify the following are listed:

| Scope | Purpose |
|---|---|
| `r_liteprofile` | Read basic member profile (id, name, picture, headline) |
| `r_emailaddress` | Read primary email address |
| `w_member_social` | Write posts (and read member posts) |
| `r_organization_social` | Read organization/company page posts |

---

## Step 4 — Set the Redirect URI

In your app settings, go to **Auth** → **Authorized redirect URLs for your app** and add:

```
https://your-shielva-domain.com/oauth/callback
```

This must match exactly what you provide in the `redirect_uri` install field.

---

## Step 5 — Get Your Credentials

In your app settings under **Auth**, note:

- **Client ID** — used as `client_id`
- **Client Secret** — used as `client_secret` (click the eye icon to reveal)

---

## Step 6 — Configure the Connector in Shielva

In the Shielva connector install form, fill in:

| Field | Key | Required | Description |
|---|---|---|---|
| OAuth Client ID | `client_id` | Yes | From LinkedIn App Credentials |
| OAuth Client Secret | `client_secret` | Yes | From LinkedIn App Credentials |
| Redirect URI | `redirect_uri` | No | Your OAuth callback URL |

---

## Step 7 — Complete the OAuth Flow

After installing the connector:

1. Shielva will redirect you to the LinkedIn consent screen.
2. Log in to LinkedIn and authorize the requested scopes.
3. LinkedIn redirects back to Shielva with an authorization code.
4. Shielva exchanges the code for an `access_token`.
5. The connector is now authorized and ready to sync.

---

## What the Connector Syncs

| Resource | Endpoint | Properties Synced |
|---|---|---|
| Profile | `GET /me` | id, firstName, lastName, headline, profilePicture |
| Email | `GET /emailAddress` | primary email address |
| Member Posts | `GET /shares?q=owners` | share id, text, author URN, visibility, timestamps |
| Organization | `GET /organizations/{id}` | id, name, vanityName, website |
| Organization Posts | `GET /shares?q=owners` | share id, text, author (org) URN, visibility, timestamps |

---

## LinkedIn URN Format

LinkedIn uses URNs (Uniform Resource Names) to identify resources:

| Resource | URN Format |
|---|---|
| Person | `urn:li:person:{id}` |
| Organization | `urn:li:organization:{id}` |
| Share/Post | `urn:li:share:{id}` |

The connector builds URNs automatically from the `id` field returned by the API.

---

## Stable Document IDs

Each document ingested into the knowledge base uses a stable ID computed as:

```
SHA-256("post:" + share_id)[:16]
SHA-256("profile:" + person_id)[:16]
```

This ensures idempotent syncs — the same resource always produces the same document ID.

---

## API Request Headers

Every request to the LinkedIn API includes:

```
Authorization: Bearer {access_token}
X-Restli-Protocol-Version: 2.0.0
LinkedIn-Version: 202401
Content-Type: application/json
```

---

## Troubleshooting

### 401 Unauthorized

- The `access_token` has expired. LinkedIn access tokens typically expire after 60 days.
- Re-authorize via the OAuth flow in Shielva to obtain a new token.
- LinkedIn does not support refresh tokens in all API product tiers; if token refresh is unavailable, re-run the authorization flow.

### 403 Forbidden — Missing Scope

- Your LinkedIn App is missing one or more required scopes.
- Go to the LinkedIn Developer Portal → Your App → Products and request the missing API product.
- After approval, re-authorize in Shielva to get a token with the new scopes.

### 429 Too Many Requests

- LinkedIn enforces rate limits (typically 100 calls/day per member for share reads).
- The connector retries automatically with exponential backoff (up to 3 attempts).
- If limits are hit repeatedly, reduce sync frequency or request a higher rate limit tier from LinkedIn.

### Connector Health is DEGRADED

- Transient network errors have tripped the circuit breaker (5 failures).
- Resolve the network or auth issue, then trigger a health check to reset.

### Posts Not Syncing

- The `w_member_social` scope is required to read and write posts.
- Ensure this scope is granted on your LinkedIn App and re-authorize.

### Organization Posts Not Syncing

- The `r_organization_social` scope is required. This requires the Marketing Developer Platform product, which requires LinkedIn approval.
- Once approved, re-authorize to obtain the scope.

---

## API Reference

- **Base URL**: `https://api.linkedin.com/v2`
- **Auth URL**: `https://www.linkedin.com/oauth/v2/authorization`
- **Token URL**: `https://www.linkedin.com/oauth/v2/accessToken`
- Profile: `GET /me?projection=(id,firstName,lastName,profilePicture,headline)`
- Email: `GET /emailAddress?q=members&projection=(elements*(handle~))`
- Posts: `GET /shares?q=owners&owners={urn}&count={count}`
- Organization: `GET /organizations/{org_id}`
- Organization Posts: `GET /shares?q=owners&owners={org_urn}&count={count}`

---

## Support

For additional help, refer to the [LinkedIn API documentation](https://docs.microsoft.com/en-us/linkedin/) or contact Shielva support.
