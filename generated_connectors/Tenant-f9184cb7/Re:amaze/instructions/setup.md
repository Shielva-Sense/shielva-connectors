# Re:amaze Connector — Setup Guide

Re:amaze is a customer support and helpdesk platform designed for eCommerce businesses. It centralizes conversations from email, live chat, social media, and SMS into one unified inbox, making it easy to support customers at scale.

## Prerequisites

- A Re:amaze account with admin access
- Your brand subdomain (the part before `.reamaze.com` in your store's URL)

## Step 1: Find your Brand Subdomain

Your Re:amaze brand subdomain is the prefix in your Re:amaze URL.

For example, if your Re:amaze URL is `https://mystore.reamaze.com`, your brand subdomain is `mystore`.

> Note: If you have multiple brands in Re:amaze, use the subdomain for the brand you want to connect.

## Step 2: Generate an API Token

1. Log in to your Re:amaze account.
2. Click your profile avatar (top-right corner) and select **Settings**.
3. In the left sidebar, navigate to **Account** → **API**.
4. Under **API Tokens**, click **Generate New Token** (or copy your existing token).
5. Copy the token — it will only be shown once.

## Step 3: Connect Re:amaze in Shielva

Fill in the following fields:

| Field | Value | Example |
|-------|-------|---------|
| **Brand Subdomain** | The prefix of your Re:amaze URL (without `.reamaze.com`) | `mystore` |
| **Email** | The email address you use to log in to Re:amaze | `admin@mystore.com` |
| **API Token** | The token you generated in Step 2 | `abc123xyz...` |

## Authentication Method

Re:amaze uses **HTTP Basic Authentication**. Shielva constructs the `Authorization` header as:

```
Authorization: Basic base64(email:api_token)
```

Your email is the username and your API token is the password.

## What Gets Synced

| Resource | Description |
|----------|-------------|
| **Conversations** | All support conversations (tickets) from every channel |
| **Contacts** | Customer profiles and contact information |
| **Articles** | Knowledge base / FAQ articles |
| **Reports** | Summary statistics (total conversations, response times, etc.) |

## Troubleshooting

**401 Unauthorized** — Double-check your email address and API token. Make sure you are using the token for the correct brand.

**Brand not found** — Confirm your brand subdomain is correct. It must match exactly the subdomain in your Re:amaze URL.

**Rate limiting** — Re:amaze enforces API rate limits. Shielva automatically retries requests with exponential backoff. If you see persistent rate limit errors, contact Re:amaze support to request a higher limit.

## Security Notes

- Your API token is stored encrypted at rest using AES-256-GCM.
- Shielva never stores your Re:amaze password — only the API token.
- You can revoke the token at any time from Re:amaze Settings → Account → API.
