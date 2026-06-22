# SurveyMonkey Connector — Setup Guide

## Overview

The SurveyMonkey connector syncs surveys, responses, collectors, contacts, and contact lists from your SurveyMonkey account into Shielva via the SurveyMonkey API v3. Authentication uses the OAuth 2.0 Authorization Code flow.

---

## Prerequisites

- A SurveyMonkey account (Enterprise plan required for `responses_read` scope on private surveys)
- Access to the [SurveyMonkey Developer Portal](https://developer.surveymonkey.com/)

---

## Step 1: Create a SurveyMonkey App

1. Go to [https://developer.surveymonkey.com/apps/](https://developer.surveymonkey.com/apps/) and sign in.
2. Click **Create new app**.
3. Enter a name (e.g., "Shielva Integration") and a brief description.
4. Under **OAuth redirect URL**, enter your Shielva callback URL:
   ```
   https://<your-shielva-domain>/connectors/surveymonkey/callback
   ```
5. Save the app.

---

## Step 2: Configure OAuth Scopes

In your app's settings, enable the following scopes:

| Scope | Purpose |
|---|---|
| `surveys_read` | List and retrieve survey definitions |
| `responses_read` | Read survey responses in bulk |

> **Note:** `responses_read` requires a **SurveyMonkey Enterprise** plan. On Advantage or lower plans, the scope will be granted but responses from private surveys will return empty results.

---

## Step 3: Copy Your Credentials

After creating the app, note these values from the app settings page:

| Field | Where to find it |
|---|---|
| **Client ID** | "Client ID" in your app settings |
| **Client Secret** | "Client Secret" in your app settings |
| **Redirect URI** | The URL you registered in Step 1 |

---

## Step 4: Install the Connector in Shielva

1. In the Shielva admin panel, go to **Connectors → Add Connector → SurveyMonkey**.
2. Enter:
   - **Client ID** — from Step 3
   - **Client Secret** — from Step 3
   - **Redirect URI** — must match the URL registered in Step 1 exactly
3. Click **Authorize**. Shielva will redirect you to SurveyMonkey's OAuth consent screen.
4. Approve access. SurveyMonkey redirects back to Shielva with an authorization code.
5. Shielva exchanges the code for an access token automatically.

---

## Resources Synced

| Resource | API Endpoint | Notes |
|---|---|---|
| Surveys | `GET /v3/surveys` | Title, question count, page count, response count |
| Survey details | `GET /v3/surveys/{id}/details` | Full page/question structure |
| Responses | `GET /v3/surveys/{id}/responses/bulk` | All submitted responses |
| Collectors | `GET /v3/surveys/{id}/collectors` | Email invitations, web links, etc. |
| Contacts | `GET /v3/contacts` | Contact directory |
| Contact lists | `GET /v3/contact_lists` | Named groups of contacts |

---

## Troubleshooting

| Error | Likely cause | Fix |
|---|---|---|
| 401 Unauthorized | Access token expired or revoked | Re-authorize the connector |
| 403 Forbidden | Insufficient OAuth scope or plan | Upgrade to Enterprise; check scopes |
| 429 Too Many Requests | API rate limit hit | The connector retries automatically with backoff |
| Empty responses | Survey on free/Advantage plan | Upgrade plan or use public surveys |

---

## Security Notes

- Client ID and Client Secret are stored encrypted in Shielva's vault.
- Access tokens are short-lived; Shielva handles token refresh automatically.
- No survey data is stored in plain text — all content is indexed only in the Shielva knowledge base.
