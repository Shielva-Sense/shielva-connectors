# Tidio Connector — Setup Guide

## Overview

The **Tidio** connector integrates Shielva with the [Tidio REST API v1](https://tidio.com/api/) to sync conversations, visitors, and chatbots into Shielva's knowledge base.

Tidio is a live chat and chatbot platform. This connector uses **API key Bearer token** authentication and exposes **7 API methods** across 5 resource domains — Conversations, Messages, Visitors, Operators, and Chatbots.

---

## Prerequisites

- A Tidio account with API access
- Your Tidio **API Key** (available from the Tidio dashboard under Settings → Integrations → API)

---

## Getting Your API Key

1. Log in to [www.tidio.com](https://www.tidio.com)
2. Navigate to **Settings → Integrations → API**
3. Generate or copy your **Public Key / API Key**
4. Keep it secure — it grants access to all your Tidio data

---

## Installing the Connector

In the Shielva ACP:

1. Navigate to **Integrations → Tidio**
2. Enter your **API Key** in the install field
3. Click **Install** — the connector validates credentials against `GET /api/v1/project`
4. Status shows **ONLINE** when successfully connected

---

## Running a Sync

The connector syncs three resource types:

| Resource | Endpoint | Notes |
|----------|----------|-------|
| Conversations | `GET /api/v1/conversations` | Paginated (page + page_size) |
| Visitors | `GET /api/v1/visitors` | Paginated |
| Chatbots | `GET /api/v1/chatbots` | Full list |

Each resource is normalized to a `ConnectorDocument` with a stable 16-character SHA-256 source ID.

---

## Quick Start (Python)

```python
import asyncio
from tidio_connector.connector import TidioConnector

async def main():
    async with TidioConnector(config={"api_key": "YOUR_API_KEY"}) as conn:
        # Health check
        health = await conn.health_check()
        print(health.message)

        # Full sync
        result = await conn.sync()
        print(f"Synced {result.documents_synced} documents")

        # List conversations
        convs = await conn.list_conversations(status="open")
        for c in convs:
            print(c["id"], c["status"])

asyncio.run(main())
```

---

## Configuration Reference

| Parameter | Key | Type | Required | Description |
|-----------|-----|------|----------|-------------|
| API Key | `api_key` | password | Yes | Tidio API key from Settings → API |

### Internal Defaults

| Parameter | Default | Description |
|-----------|---------|-------------|
| `API_BASE` | `https://api.tidio.co` | Tidio API base URL |
| `SYNC_PAGE_SIZE` | `50` | Items per page for sync |
| `TIMEOUT` | `30s` | HTTP request timeout |
| `MAX_RETRIES` | `3` | Retry attempts on transient failures |
| `RETRY_BACKOFF` | Exponential | 1s → 2s → 4s + jitter, max 30s |

---

## Error Reference

| Exception | Trigger | Retried? |
|-----------|---------|----------|
| `TidioAuthError` | 401 / 403 response | Never |
| `TidioRateLimitError` | 429 response | Yes (honours `retry_after`) |
| `TidioNotFoundError` | 404 response | Yes |
| `TidioNetworkError` | 5xx / connection error | Yes |
| `TidioError` | Any other API error | Yes |

---

## Security Notes

- API keys are stored encrypted in the Shielva vault and never logged
- All requests go over HTTPS to `https://api.tidio.co`
- Rotate keys in Tidio → Settings → API, then re-install the connector in Shielva ACP
