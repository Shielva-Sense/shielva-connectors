# Lattice Connector — Setup Guide

## Overview

The **Lattice** connector integrates Shielva with the [Lattice REST API v1](https://developers.latticehq.com/) to sync employees, goals/OKRs, and performance reviews into Shielva's knowledge base.

Lattice is a leading people management and performance platform. This connector exposes 6 list/fetch methods across 5 resource domains — Users, Departments, Goals, Reviews, Feedback — and implements a full sync engine that ingests normalized `ConnectorDocument` objects.

---

## Prerequisites

- An active Lattice account with admin access
- A Lattice API token (see below)

---

## Step 1: Generate a Lattice API Token

1. Log in to your Lattice account at `https://lattice.com`
2. Navigate to **Settings → Integrations → API**
3. Click **Create API Token**
4. Give the token a descriptive name (e.g. `shielva-connector`)
5. Copy the token — it is shown only once

---

## Step 2: Install the Connector in Shielva

1. Navigate to **Integrations → Lattice** in the Shielva ACP
2. Enter your **API Token** in the install form
3. Click **Install** — status shows **ONLINE** on success

---

## Step 3: Run Your First Sync

```python
from connector import LatticeConnector

async with LatticeConnector(api_token="your_token_here") as conn:
    result = await conn.sync(full=True)
    print(f"Found: {result.documents_found}")
    print(f"Synced: {result.documents_synced}")
    print(f"Failed: {result.documents_failed}")
```

---

## Authentication

The connector uses **Bearer token authentication**. Every request carries:

```
Authorization: Bearer {api_token}
```

| Field | Key | Type | Required |
|-------|-----|------|----------|
| API Token | `api_token` | password | Yes |

---

## Synced Resources

| Resource | Lattice Endpoint | Document Type |
|----------|-----------------|---------------|
| Employees | `GET /v1/users` | `employee` |
| Goals/OKRs | `GET /v1/goals` | `goal` |
| Performance Reviews | `GET /v1/reviews` | `performance_review` |

Feedback and departments are also available via `list_feedback()` and `list_departments()` but are not included in the default sync.

---

## Error Reference

| Error | HTTP Status | Meaning |
|-------|-------------|---------|
| `LatticeAuthError` | 401/403 | Invalid or expired API token |
| `LatticeNotFoundError` | 404 | Resource does not exist |
| `LatticeRateLimitError` | 429 | Too many requests — auto-retried |
| `LatticeNetworkError` | 5xx / timeout | Transient network failure — auto-retried |

---

## Retry Behavior

| Error | Retried? |
|-------|----------|
| `LatticeNetworkError` | Yes — exponential backoff (1 s, 2 s, 4 s + jitter) |
| `LatticeRateLimitError` | Yes — respects `Retry-After` header |
| `LatticeAuthError` | No — requires manual credential fix |
| `LatticeNotFoundError` | No — resource is gone |

---

## Running Tests

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/lattice_connector
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
python -m pytest tests/ -v
```

Expected: 60+ tests, all pass.
