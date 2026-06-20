# Twilio Connector — Setup Guide

## Overview

The Twilio connector syncs SMS messages, voice calls, and phone numbers from your Twilio account into Shielva.

---

## Prerequisites

- A Twilio account (trial or paid) at [console.twilio.com](https://console.twilio.com)
- At least one phone number provisioned (required for SMS/call data to exist)

---

## Getting Your Credentials

Both required credentials are displayed on the **Account Info** panel of the Twilio Console dashboard.

1. Log in to [console.twilio.com](https://console.twilio.com)
2. On the main dashboard you will see the **Account Info** panel in the lower-left area
3. Copy the **Account SID** — it starts with `AC` followed by 32 hex characters
4. Click **show** next to **Auth Token** and copy the value

> Keep your Auth Token secret. If it is compromised, rotate it immediately under Account Settings → Auth Tokens.

---

## Installation Fields

| Field | Key | Example | Notes |
|-------|-----|---------|-------|
| Account SID | `account_sid` | `ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` | Always starts with `AC` |
| Auth Token | `auth_token` | `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` | 32-character hex string |

---

## What Gets Synced

| Resource | API Endpoint | Notes |
|----------|-------------|-------|
| SMS Messages | `GET /Messages.json` | Full history, paginated |
| Voice Calls | `GET /Calls.json` | Full history, paginated |
| Phone Numbers | `GET /IncomingPhoneNumbers.json` | Your provisioned numbers |

---

## Troubleshooting

### Error code 20003 — Invalid credentials

The Account SID or Auth Token is incorrect. Double-check both values in the Twilio Console. Make sure you are copying the **Auth Token** (not the signing key or API key).

### Error code 20429 — Rate limit exceeded

Twilio limits API requests. The connector automatically retries with exponential backoff. If this error persists, reduce sync frequency.

### No messages or calls returned

Your account has no SMS or call history yet. You need to:
- Buy a Twilio phone number if you have not already
- Send a test SMS or make a test call via the Twilio Console or API

### No phone numbers returned

You have not purchased any Twilio phone numbers. Go to **Phone Numbers → Manage → Buy a number** in the Twilio Console.

### Trial account limitations

On a trial account, you can only send messages/calls to verified numbers. The connector reads data fine, but your history will be limited to trial activity.
