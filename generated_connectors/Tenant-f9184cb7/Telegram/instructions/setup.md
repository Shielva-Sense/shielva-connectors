# Setup Instructions: Telegram

## Overview

The Telegram connector integrates a Telegram bot account with the Shielva
platform. Once connected, Shielva can send, edit, delete, and forward messages,
manage webhooks, poll for incoming updates, and inspect chats and members.
Telegram authenticates bots with a single **bot token** that is embedded in
every request URL — there is no OAuth flow and no separate refresh token.

---

## Prerequisites

- A Telegram account (to talk to **@BotFather**).
- HTTPS endpoint if you plan to use the webhook flow (`set_webhook`).
- A target chat or channel where the bot is a member (groups and channels
  must explicitly add the bot).

---

## Step-by-Step Configuration

### Step 1: Bot Token (`bot_token`) — **Required**

1. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to choose a display name and a
   `@username` (must end in `bot`, e.g. `myteam_alerts_bot`).
3. BotFather replies with a token in the format
   `123456789:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`. Copy it.
4. Paste this value into the **Bot Token** field in Shielva. The field is
   stored encrypted.

> **Tip:** treat the bot token like a password. Anyone with the token can
> impersonate the bot. Rotate via BotFather `/revoke` if leaked.

---

### Step 2: Telegram API Base URL (`base_url`) — **Optional**

- **Default:** `https://api.telegram.org`
- Override only when routing through an internal proxy or a self-hosted
  [Bot API server](https://github.com/tdlib/telegram-bot-api).

---

### Step 3: Default Parse Mode (`default_parse_mode`) — **Optional**

- **Default:** `HTML`
- Supported values: `HTML`, `MarkdownV2`, `Markdown`.
- Individual API calls (`send_message`, `edit_message`, `send_photo`,
  `send_document`) can override this per call.

---

### Step 4: Rate Limit (`rate_limit_per_min`) — **Optional**

- **Default:** `1800` (≈ 30 messages/second, Telegram's documented global
  cap across all chats).
- Lower this if your bot operates in a quota-controlled environment.
- The connector honors `parameters.retry_after` from any `429` response
  automatically — there is no need to throttle manually.

---

## Completing Installation

Click **Save** in the Shielva connector dashboard. The connector probes
`/getMe` to validate the token. A green **Connected** badge means the token
was accepted.

If the badge is red:

- **Missing Credentials** — `bot_token` is blank.
- **Token expired / 401** — the token was revoked or mistyped; regenerate
  it with `/token` in BotFather and re-save.

---

## Webhook vs Long Polling

The connector supports both ingestion modes:

- **Webhook** — call `set_webhook(url, secret_token, allowed_updates)` with
  your HTTPS endpoint. Telegram echoes `secret_token` in the
  `X-Telegram-Bot-Api-Secret-Token` header so your receiver can authenticate
  inbound requests.
- **Polling** — leave the webhook unset (`delete_webhook` if previously set)
  and call `sync()` periodically, or call `get_updates(timeout=30)` for long
  polling.

---

## Testing the Connection

1. Click **Run Health Check** — invokes `/getMe`.
2. Open **APIs → send_message**, fill in `chat_id` (your own user ID is
   fine for the first test) and `text`, then click **Run**. The connector
   returns the Telegram `Message` object with the assigned `message_id`.
3. Use `get_updates` after sending a message to the bot in Telegram to
   verify polling works.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` on every call | Bot token is wrong or was revoked | Regenerate via @BotFather and update the field |
| `400 chat not found` | Bot has never received a message from this user, or it isn't a member of the chat | Have the user `/start` the bot, or add the bot to the group/channel |
| `403 bot was blocked by the user` | The user removed the bot from their chat list | Cannot send to that user — surface to the operator |
| `429 Too Many Requests` | Hitting per-chat or global flood limits | The connector automatically waits `retry_after`; lower send frequency if recurring |
| `webhook` not receiving updates | URL is not HTTPS, or certificate isn't trusted | Use a public HTTPS endpoint with a real CA certificate; check `get_webhook_info` for `last_error_message` |
| Inline keyboard buttons do nothing | Bot didn't call `answer_callback_query` | Every callback query must be acknowledged within ~15 seconds |
