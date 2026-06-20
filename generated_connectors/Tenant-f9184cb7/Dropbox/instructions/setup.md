# Dropbox Connector — Setup Guide

## Overview

The Dropbox connector syncs all files and folders from a Dropbox account into the Shielva knowledge base using the Dropbox API v2. It uses OAuth2 Authorization Code flow for authentication.

---

## Prerequisites

- A Dropbox account (personal or business)
- A Dropbox App registered at [https://www.dropbox.com/developers/apps](https://www.dropbox.com/developers/apps)
- Python 3.11+
- The Shielva `.venv` activated

---

## Step 1 — Create a Dropbox App

1. Go to [https://www.dropbox.com/developers/apps](https://www.dropbox.com/developers/apps) and click **Create app**.
2. Choose **Scoped access** → **Full Dropbox** (or **App folder** if scoped).
3. Name your app and click **Create app**.
4. On the app settings page, copy:
   - **App key** → used as `app_key`
   - **App secret** → used as `app_secret`
5. Under **Permissions**, enable:
   - `files.content.read`
   - `files.metadata.read`
   - `account_info.read`
6. Under **OAuth 2 / Redirect URIs**, add your redirect URI (e.g. `https://yourapp.com/oauth/callback`).

---

## Step 2 — Install Dependencies

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/dropbox_connector
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
pip install -r requirements.txt
```

---

## Step 3 — Configure the Connector

Create a `config` dict (or pass to the connector at runtime):

```python
config = {
    "app_key": "<your_app_key>",
    "app_secret": "<your_app_secret>",
    "redirect_uri": "https://yourapp.com/oauth/callback",  # optional
    "access_token": "",  # populated after OAuth flow completes
}
```

---

## Step 4 — OAuth2 Authorization Flow

```python
import asyncio
from connector import DropboxConnector

connector = DropboxConnector(config=config)

# 1. Install — validates app credentials
result = asyncio.run(connector.install())
print(result.message)

# 2. Get the authorization URL
auth_url = connector.authorize()
print("Visit:", auth_url)

# 3. User visits auth_url, approves, and Dropbox redirects to your redirect_uri
#    with ?code=<authorization_code>
#    Exchange the code for an access_token at:
#    POST https://api.dropboxapi.com/oauth2/token
#    body: grant_type=authorization_code, code=<code>, client_id=<app_key>,
#          client_secret=<app_secret>, redirect_uri=<redirect_uri>
#
# 4. Store the returned access_token in config["access_token"]
config["access_token"] = "<token_from_oauth_exchange>"
connector = DropboxConnector(config=config)
```

---

## Step 5 — Health Check

```python
result = asyncio.run(connector.health_check())
print(result.health, result.display_name, result.email)
```

---

## Step 6 — Sync Files

```python
result = asyncio.run(connector.sync(full=True, kb_id="your_kb_id"))
print(f"Found: {result.documents_found}, Synced: {result.documents_synced}")
```

---

## Step 7 — Run Tests

```bash
cd /Users/vivekvarshavaishvik/Documents/client_dir/dropbox_connector
source "/Volumes/V3-SSD/Shielva Project Dirs/.venv/bin/activate"
python -m pytest tests/ -v
```

Expected output: **105 passed**.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `install()` | — | Validates app credentials; verifies access_token if present |
| `authorize()` | — | Returns Dropbox OAuth2 authorization URL |
| `health_check()` | POST /users/get_current_account | Returns health status, display name, email |
| `sync()` | POST /files/list_folder (recursive) | Full sync of all files and folders |
| `list_folder(path, recursive)` | POST /files/list_folder | List folder contents |
| `list_folder_continue(cursor)` | POST /files/list_folder/continue | Paginate with cursor |
| `get_metadata(path)` | POST /files/get_metadata | File/folder metadata by path |
| `search_files(query, max_results)` | POST /files/search_v2 | Full-text file search |

---

## Key Technical Details

- All Dropbox API v2 calls use **POST** with a JSON body to `https://api.dropboxapi.com/2/`
- Pagination uses `has_more` + `cursor` fields (not offset-based)
- `normalize_file_metadata` produces a `ConnectorDocument` with:
  - `source_id`: SHA-256(dropbox_file_id)[:16] — stable across renames/moves
  - `metadata.type`: `dropbox_file` or `dropbox_folder`
- Retry: exponential backoff with jitter, up to 3 attempts; auth errors never retried
- Circuit breaker: opens after 5 consecutive failures, recovers after 60 s

---

## Registration (Shielva ACP)

```bash
curl -sk -X POST "https://localhost:8055/sessions/import-existing" \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: Tenant-f9184cb7" \
  -H "X-App-ID: 91f5d9b2486a3610" \
  -d '{
    "connectors": [{
      "service_slug": "dropbox",
      "provider": "dropbox",
      "service": "dropbox",
      "connector_name": "Dropbox",
      "version": "1.0.0",
      "run_kind": "build",
      "output_dir": "/Users/vivekvarshavaishvik/Documents/client_dir/dropbox_connector"
    }]
  }' | python3 -m json.tool
```

---

## Scopes Required

```
files.content.read
files.metadata.read
account_info.read
```
