# Trello Connector — Setup Guide

## Overview

The Trello connector syncs your Trello boards and cards into the Shielva knowledge base using the **Trello REST API v1** (base URL: `https://api.trello.com/1/`). Authentication uses an API key and an OAuth token, both appended as query parameters on every request — Trello does not use the `Authorization` header.

Provider: **Atlassian** | Service: **Trello** | Auth type: `api_key`

---

## Step 1 — Obtain Your API Key

1. Log in to your Trello account at [https://trello.com](https://trello.com).
2. Visit [https://trello.com/app-key](https://trello.com/app-key).
3. Your **API Key** (32 characters) is shown at the top of the page.
4. Copy and save it — you will paste it into the **API Key** field in Shielva.

> **Power-Ups vs. direct API:** The API key on trello.com/app-key is for direct REST API access. Trello Power-Ups use a different flow. For Shielva, always use the direct API key.

---

## Step 2 — Generate an OAuth Token

1. On the same page ([https://trello.com/app-key](https://trello.com/app-key)), scroll down and click the **Token** link (or visit: `https://trello.com/1/authorize?expiration=never&scope=read&response_type=token&name=Shielva&key=YOUR_API_KEY`).
2. A Trello authorization page opens. Review the requested permissions (read access to your boards and organizations).
3. Click **Allow**.
4. Copy the 64-character token shown on the confirmation page. **Store it securely** — Trello will not display it again.

> **Token expiry:** Using `expiration=never` creates a non-expiring token. You may choose `expiration=30days` or `expiration=1day` for shorter-lived tokens. If your token expires, repeat this step to generate a new one.

> **Read permission is sufficient:** The connector only reads from Trello. No write or admin scope is required.

---

## Step 3 — Find Board IDs (optional)

Board IDs appear in the Trello board URL:

```
https://trello.com/b/{BOARD_ID}/{board-name}
```

For example, `https://trello.com/b/abc12345/my-project` → board ID is `abc12345`.

You can also retrieve all board IDs by calling `list_boards()` after installation.

---

## Step 4 — Install in Shielva

In the Shielva connector install form, provide both fields:

| Field | Key | Type | Required | Where to find |
|---|---|---|---|---|
| API Key | `api_key` | string | Yes | [trello.com/app-key](https://trello.com/app-key) — top of page |
| Token | `token` | password | Yes | [trello.com/app-key](https://trello.com/app-key) → click "Token" → Allow |

The connector validates credentials by calling `GET /members/me?key={api_key}&token={token}`. On success, status is set to **Connected** and displays your Trello username.

---

## Authentication Details

Trello authenticates via query parameters on every request — not via the `Authorization` header:

```
GET https://api.trello.com/1/members/me?key=YOUR_API_KEY&token=YOUR_TOKEN&fields=id,username,fullName,email
```

The `TrelloHTTPClient._auth_params()` method returns `{"key": api_key, "token": token}` and merges them into every request automatically.

---

## What Gets Synced

| Resource | API endpoint | Document type |
|---|---|---|
| Boards | `GET /members/me/boards?filter=open` | `board` |
| Cards | `GET /boards/{id}/cards/open` | `card` |
| Lists | `GET /boards/{id}/lists?filter=open` | (via list_board_lists) |
| Members | `GET /boards/{id}/members` | (via list_board_members) |
| Labels | `GET /boards/{id}/labels` | (via list_board_labels) |

Each board is normalized into a `ConnectorDocument` with `id = sha256("board:" + board_id)[:16]`, `source = "trello"`, `type = "board"`.

Each card is normalized into a `ConnectorDocument` with `id = sha256("card:" + card_id)[:16]`, `source = "trello"`, `type = "card"`.

---

## Rate Limits

Trello enforces a limit of **300 requests per 10 seconds** per token. The connector retries automatically with exponential backoff (up to 3 attempts). If you hit rate limits frequently during large syncs, space out sync jobs or reduce the sync frequency.

---

## API Methods Reference

| Method | HTTP | Path | Description |
|---|---|---|---|
| `get_member(member_id)` | GET | `/members/{id}` | Fetch member info; used for auth validation |
| `list_boards(member_id, filter)` | GET | `/members/{id}/boards` | List boards for a member |
| `get_board(board_id, fields)` | GET | `/boards/{id}` | Fetch single board |
| `list_board_lists(board_id, filter)` | GET | `/boards/{id}/lists` | Fetch lists on a board |
| `list_board_cards(board_id, filter)` | GET | `/boards/{id}/cards/{filter}` | Fetch cards on a board |
| `get_card(card_id)` | GET | `/cards/{id}` | Fetch single card (all fields) |
| `list_board_members(board_id)` | GET | `/boards/{id}/members` | Fetch members of a board |
| `list_board_labels(board_id)` | GET | `/boards/{id}/labels` | Fetch labels defined on a board |

Note: Trello does not paginate most endpoints — it returns all results in a single response. Cards and actions support `before`/`since` date filtering but the connector uses filter path segment (`open`, `all`, etc.) instead.

---

## Troubleshooting

### 401 Unauthorized — Invalid API Key or Token

- Verify your API key at [https://trello.com/app-key](https://trello.com/app-key).
- Regenerate a new token by clicking the Token link and re-authorizing.
- Ensure you copied the full 64-character token without trailing spaces.

### 403 Forbidden — Insufficient Permissions

- The token was generated with restricted permissions.
- Regenerate a token with at least **read** scope.

### 429 Too Many Requests — Rate Limited

- Trello rate limit: 300 requests per 10 seconds per token.
- The connector retries with exponential backoff (3 attempts).
- For large workspaces, consider scheduling syncs during off-peak hours.

### 404 Not Found

- The board ID or card ID does not exist or is not accessible to the authenticated user.
- Verify the resource exists and the token has access to it.

### Token Expired

- If you chose a short expiry when generating the token, it has now expired.
- Return to [https://trello.com/app-key](https://trello.com/app-key), click **Token**, and re-authorize.

---

## Full API Documentation

[https://developer.atlassian.com/cloud/trello/rest/](https://developer.atlassian.com/cloud/trello/rest/)
