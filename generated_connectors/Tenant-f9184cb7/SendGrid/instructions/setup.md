# SendGrid Connector — Setup Guide

## Overview

The SendGrid connector syncs your email activity stats, marketing contacts, email templates, suppression lists, and segment data into Shielva using the SendGrid Web API v3. SendGrid is Twilio's email delivery platform for both transactional and marketing email at scale.

---

## Prerequisites

- A SendGrid account (free Essentials plan or higher)
- Marketing Campaigns enabled (required for contacts, lists, and segments)
- A verified sender identity or authenticated domain (required to send email)

---

## Step 1 — Create a SendGrid Account

1. Go to [sendgrid.com](https://sendgrid.com) and click **Start for Free**
2. Complete email verification for your account
3. You will start on the free Essentials plan (100 emails/day)
4. Upgrade to a paid plan for higher volume or advanced marketing features

---

## Step 2 — Sender Authentication / Domain Verification

SendGrid requires sender authentication before you can send email. There are two options:

### Option A — Single Sender Verification (quickest)
1. In SendGrid → **Settings → Sender Authentication**
2. Click **Verify a Single Sender**
3. Fill in your From Name, From Email, Reply To, and physical address
4. Click **Create** — a verification email is sent to the From Email address
5. Click the link in the verification email

### Option B — Domain Authentication (recommended for production)
1. In SendGrid → **Settings → Sender Authentication**
2. Click **Authenticate Your Domain**
3. Select your DNS provider (or choose "Other" for manual setup)
4. SendGrid generates DNS records (CNAME entries) to add to your domain
5. Add all provided DNS records to your DNS provider
6. Return to SendGrid and click **Verify**
7. Authentication typically takes 5–10 minutes after DNS propagation

Domain authentication improves deliverability and removes the "via sendgrid.net" annotation from sent emails.

---

## Step 3 — Create an API Key in SendGrid

1. Log in to [app.sendgrid.com](https://app.sendgrid.com)
2. Go to **Settings → API Keys**
3. Click **Create API Key**
4. Choose **Full Access** (recommended) or **Restricted Access** with the following scopes enabled:
   - Mail Send → `mail.send`
   - Marketing → `marketing.contacts.read`, `marketing.lists.read`, `marketing.segments.read`
   - Templates → `templates.read`, `templates.versions.read`
   - Suppressions → `suppression.read`, `suppression.groups.read`
   - Stats → `stats.read`
   - User → `user.profile.read`
5. Give the key a descriptive name (e.g. `Shielva Connector`)
6. Click **Create & View** — copy the key immediately (it is only shown once)

> **Tip:** Store the key securely. You cannot retrieve it again from SendGrid after closing the creation dialog. API keys always start with `SG.`.

---

## Step 4 — Install the Connector in Shielva

1. In Shielva, navigate to **Integrations → Email → SendGrid**
2. Click **Connect**
3. Paste your API Key into the **API Key** field
4. Click **Install**

The connector verifies your key against `GET /user/profile`. On success, status shows **Connected**.

---

## Step 5 — Run Your First Sync

After installation, the connector can:

- Sync all marketing contacts (`GET /marketing/contacts`)
- Sync all email templates (`GET /templates`)
- List marketing lists and segments (`GET /marketing/lists`, `GET /marketing/segments/2.0`)
- Fetch email activity stats for any date range (`GET /stats`)
- List global suppression emails (`GET /asm/suppressions/global`)

Sync runs automatically on the configured schedule, or you can trigger it manually from the connector settings.

---

## Troubleshooting

### 401 Unauthorized after install

Your API key is invalid or has been revoked.

**Fix:** Go to SendGrid → Settings → API Keys, generate a new key, and update the connector credentials in Shielva.

### 403 Forbidden on contacts or templates

Your API key does not have the required scopes.

**Fix:** Create a new key with Full Access, or ensure the restricted key has the Marketing and Templates scopes enabled (see Step 1).

### Contacts not appearing after sync

Marketing Campaigns may not be enabled on your SendGrid account.

**Fix:** In SendGrid, go to **Marketing → Contacts** and follow the prompt to enable Marketing Campaigns. Free accounts have a 2,000-contact limit.

### 429 Too Many Requests

You are hitting SendGrid's rate limits. The connector retries automatically with exponential backoff.

**Fix:** If this is persistent, reduce the sync frequency in connector settings or contact SendGrid support to increase your rate limits.

### Templates returning empty list

Dynamic templates are only available on plans that include the Dynamic Transactional Templates feature.

**Fix:** Check your SendGrid plan. Legacy templates are available on all plans and are included by default when `generations=legacy,dynamic` is used.

---

## API Reference

- [SendGrid Web API v3 Documentation](https://docs.sendgrid.com/api-reference)
- [API Keys Guide](https://docs.sendgrid.com/ui/account-and-settings/api-keys)
- [Marketing Contacts](https://docs.sendgrid.com/api-reference/contacts)
- [Email Templates](https://docs.sendgrid.com/api-reference/transactional-templates)
- [Suppression Bounces](https://docs.sendgrid.com/api-reference/bounces-api)
