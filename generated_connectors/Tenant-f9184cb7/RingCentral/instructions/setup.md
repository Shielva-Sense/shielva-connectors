# RingCentral Connector — Setup Guide

## Overview

This connector syncs call logs, messages, extensions, contacts, and meetings from the
RingCentral cloud communications platform using the RingCentral REST API v1 and OAuth 2.0.

---

## Step 1 — Create a RingCentral Developer App

1. Go to [https://developers.ringcentral.com](https://developers.ringcentral.com) and sign in.
2. Click **Create App**.
3. Choose **REST API App** as the app type.
4. Set **Auth type** to **OAuth 2.0 — Authorization Code Flow**.
5. Fill in the required fields:
   - **App Name**: e.g. `Shielva RingCentral Connector`
   - **App Description**: Data sync integration
   - **Redirect URI**: your Shielva connector callback URL (e.g. `https://<your-domain>/connectors/ringcentral/callback`)
6. Under **OAuth Scopes**, add the following permissions:
   - `ReadCallLog` — Access call logs
   - `ReadMessages` — Access messages (SMS, Fax, Voicemail)
   - `ReadContacts` — Access personal and company contacts
   - `Meetings` — Access meeting data (if your plan includes RingCentral Video / Meetings)
7. Click **Create App**.

---

## Step 2 — Note Your Credentials

After creating the app, copy:

| Field | Where to find it |
|---|---|
| **Client ID** | App dashboard → "Credentials" tab |
| **Client Secret** | App dashboard → "Credentials" tab (click Reveal) |

Keep these secret. Never commit them to source control.

---

## Step 3 — Sandbox vs Production

RingCentral provides two environments:

| Environment | Server URL | Use case |
|---|---|---|
| **Sandbox** | `https://platform.devtest.ringcentral.com` | Development & testing |
| **Production** | `https://platform.ringcentral.com` | Live customer data |

When installing the connector in Shielva:
- Leave **Server URL** empty (or enter the production URL) for live accounts.
- Enter `https://platform.devtest.ringcentral.com` to test with sandbox credentials.

Sandbox credentials and production credentials are **separate** — you must create test users in the sandbox account.

---

## Step 4 — Install in Shielva

1. Open the Shielva ARC connector catalogue.
2. Click **RingCentral** → **Install**.
3. Enter:
   - **Client ID**: from Step 2
   - **Client Secret**: from Step 2
   - **Server URL**: leave blank for production, or enter the sandbox URL for testing
4. Click **Authorize** — you will be redirected to RingCentral's OAuth consent screen.
5. Sign in with a RingCentral account that has Admin or Super Admin permissions.
6. Grant the requested permissions.
7. You will be redirected back to Shielva with the connection active.

---

## Step 5 — Verify the Connection

After authorization, Shielva performs a health check by calling:

```
GET {server_url}/restapi/v1.0/account/~/extension/~
```

A `200 OK` response confirms the connection is working. If the check fails:

- Verify the Client ID and Client Secret are correct.
- Ensure the OAuth scopes include `ReadCallLog`, `ReadMessages`, and `ReadContacts`.
- Check that the authorized account has the required permissions in RingCentral.

---

## Required RingCentral Plan

| Feature | Minimum plan |
|---|---|
| Call logs | Any RingCentral MVP / Office plan |
| Messages (SMS) | Plans that include SMS |
| Contacts | Any plan |
| Meetings | RingCentral Video (included in MVP/Office) |

---

## Security Notes

- The connector stores your access token and refresh token encrypted at rest using Shielva's vault.
- Tokens are never logged.
- The refresh token is used automatically to obtain new access tokens — you will not need to re-authorize unless you revoke the app's access in the RingCentral Developer Portal.
- To revoke access: go to your RingCentral account → **Settings** → **Authorized Apps** → revoke `Shielva RingCentral Connector`.
