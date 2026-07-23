"""Integration Builder — CONNECTOR_DOCUMENTATION_GUIDELINES service.

Manages connector_documentation.md: the standard documentation template
that defines how connector documentation should be structured.
Storage hierarchy: Redis cache -> R2 bucket -> embedded default.
Every save creates a new MongoDB version record.
"""

import asyncio
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import structlog

from integration.core.config import settings

logger = structlog.get_logger(__name__)

# R2 key prefix for documentation guidelines
# Full path in bucket: {R2_COLLECTION_PREFIX}/CONNECTOR_DOCUMENTATION_GUIDELINES/...
# e.g.  shielvasense-integration-plans/CONNECTOR_DOCUMENTATION_GUIDELINES/connector_documentation.md
_DOC_GUIDELINES_R2_PREFIX = f"{settings.R2_COLLECTION_PREFIX}/CONNECTOR_DOCUMENTATION_GUIDELINES"
_STANDARD_KEY = "connector_documentation.md"
_VERSIONED_KEY_TPL = "connector_doc_v_{version}.md"

# Redis cache key template
_REDIS_KEY_TPL = "connector_doc_guidelines:v{version}"
_ACTIVE_VERSION_KEY = "connector_doc_guidelines:active_version"

# ── Default connector_documentation.md ───────────────────────────────

DEFAULT_CONNECTOR_DOCUMENTATION_MD = """\
# Connector Documentation Standard

> Managed by Shielva Integration Builder — Comprehensive Documentation Guidelines v2.0

This template defines the full documentation standard every Shielva connector must satisfy.
Gemini MUST fill in every section with connector-specific, accurate, production-quality content.
Generic placeholders are not acceptable. Every section must reflect the real connector.

---

## 1. Overview

### 1.1 What This Connector Does
[Describe exactly what external service this connector integrates with, what data it reads/writes,
what business problem it solves, and what makes it unique compared to direct API access.]

### 1.2 Connector Identity
| Field | Value |
|-------|-------|
| Provider | [e.g. Google, Slack, Salesforce] |
| Service | [e.g. Gmail, Channels, CRM] |
| Connector Class | [e.g. GmailConnector] |
| Base Class | BaseConnector (shared.base_connector) |
| Auth Type | [OAuth2 / API Key / Basic / Bearer Token] |
| Sync Type | [Full + Incremental / Incremental only / Push (webhook)] |
| Created | [YYYY-MM-DD] |
| Current Version | [semver e.g. 1.0.0] |

### 1.3 Target Audience
- Developers integrating this connector into Shielva workflows
- Platform administrators deploying or upgrading connectors
- QA engineers running test suites and validating coverage
- End users configuring the connector via the CMS UI

### 1.4 Key Capabilities
| Capability | Supported | Notes |
|-----------|-----------|-------|
| Full Sync | Yes/No | [describe scope] |
| Incremental Sync | Yes/No | [cursor strategy used] |
| Webhook / Real-time | Yes/No | [event types] |
| OAuth2 Token Refresh | Yes/No | [auto-refresh on expiry] |
| Rate Limit Handling | Yes/No | [backoff strategy] |
| Pagination | Yes/No | [cursor / page / offset] |
| Multi-account | Yes/No | [per tenant_id] |
| Bulk Operations | Yes/No | [batch size] |

### 1.5 Architecture Diagram
```
CMS UI → Shielva Gateway → ConnectorService
                                  ↓
                          connector.py  ←→  external API
                                  ↓
                          shared/base_connector.py
                                  ↓
                          Redis (token store) + MongoDB (sync state)
```

---

## 2. Prerequisites

### 2.1 Required Accounts & Access
- [ ] Active account on [Provider] (e.g. Google Workspace Admin, Slack workspace owner)
- [ ] Permission to create OAuth apps / API credentials in the provider's developer console
- [ ] Shielva platform tenant account with Integration Builder access

### 2.2 Provider Developer Console Setup
Step-by-step for creating credentials:
1. Go to [Provider Developer Console URL]
2. Create a new project/app named (suggested): `Shielva Integration`
3. Enable APIs: [list exact API names, e.g. "Gmail API", "Google People API"]
4. Create OAuth 2.0 credentials (Client ID + Secret) or API key
5. Set the Authorized Redirect URI to: `https://{your-shielva-domain}/api/v1/oauth/callback`
6. Download or copy: `client_id`, `client_secret` (keep secret — never commit)

### 2.3 Required OAuth Scopes / API Permissions
| Scope / Permission | Purpose | Minimum Required |
|-------------------|---------|-----------------|
| `scope.read` | Read [resource] from [service] | Yes |
| `scope.write` | Write/update [resource] | Only for write methods |
| `scope.admin` | Access admin-level data | No (optional) |

### 2.4 Network & Firewall Requirements
| Direction | Protocol | Host | Port | Purpose |
|-----------|----------|------|------|---------|
| Outbound | HTTPS | `api.service.com` | 443 | API calls |
| Outbound | HTTPS | `oauth2.service.com` | 443 | OAuth token exchange |

No inbound ports are required.

---

## 3. Installation & Configuration

### 3.1 Install via CMS
1. Navigate to **CMS > Integrations > [Provider] > [Service]**
2. Click **"New Connector"** and enter a name
3. Fill in credentials:
   - `client_id`: From provider developer console
   - `client_secret`: From provider developer console
   - [Any other required fields]
4. Click **"Authorize"** — you will be redirected to the provider's OAuth consent screen
5. Grant the requested permissions
6. You are redirected back to CMS — connector is now installed

### 3.2 Configuration Schema
Every field in `metadata/connector.json` config block, with type, default, and validation rules:

```json
{
  "client_id":      { "type": "text",     "required": true,  "label": "OAuth Client ID" },
  "client_secret":  { "type": "password", "required": true,  "label": "OAuth Client Secret" },
  "sync_interval":  { "type": "number",   "required": false, "default": 3600,  "label": "Sync Interval (seconds)" },
  "batch_size":     { "type": "number",   "required": false, "default": 100,   "label": "Records per sync page" },
  "base_url":       { "type": "text",     "required": false, "default": "https://api.service.com", "label": "API Base URL" }
}
```

### 3.3 Environment Variables
These must be set in the connector service's `.env`:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SERVICE_BASE_URL` | No | `https://api.service.com` | Override API base URL |
| `REQUEST_TIMEOUT` | No | `60` | HTTP timeout in seconds |
| `MAX_RETRIES` | No | `3` | Max retry attempts on transient failure |
| `SYNC_BATCH_SIZE` | No | `100` | Default records per page if not in config |

### 3.4 Token Storage
Tokens are NEVER stored in files, instance variables, or logs.
All credentials flow through `BaseConnector.set_token()` / `get_token()` which stores them
encrypted in Redis under the key `connector:{tenant_id}:{connector_id}:tokens`.

```python
# Store after OAuth exchange:
await self.set_token("access_token", token_response["access_token"])
await self.set_token("refresh_token", token_response["refresh_token"])
await self.set_token("expires_at", str(expires_at_unix))

# Read before each API call:
access_token = await self.get_token("access_token")
```

---

## 4. API Reference

### 4.1 BaseConnector Interface
All methods below are required by `BaseConnector`. The connector MUST implement each one.

---

#### `initialize(config: Dict[str, Any]) -> None`
Sets up the connector instance with tenant-specific configuration. Called once during install.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `config` | `Dict[str, Any]` | Yes | Flat dict of all connector config fields |

**Behaviour:** Validates required keys, stores config on `self`, does NOT call the external API.
**Raises:** `ConfigurationError` if required keys are missing.

---

#### `authorize(credentials: Dict[str, Any]) -> AuthStatus`
Exchanges an OAuth2 authorization code (or refreshes an existing token) and persists tokens.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `code` | `str` | Yes (initial) | OAuth2 authorization code from provider callback |
| `refresh_token` | `str` | Yes (refresh) | Existing refresh token |

**Returns:** `AuthStatus`
```json
{
  "authenticated": true,
  "user_email": "user@example.com",
  "expires_at": "2024-06-01T12:00:00Z",
  "scopes": ["scope1", "scope2"]
}
```
**Raises:** `AuthenticationError` on invalid code or revoked token.

---

#### `health_check() -> ConnectorHealth`
Verifies the connector can reach the external API with current credentials.

**Parameters:** None
**Returns:** `ConnectorHealth`
```json
{
  "status": "healthy",
  "message": "Connected successfully",
  "latency_ms": 142,
  "last_checked": "2024-06-01T10:00:00Z"
}
```
**Status values:** `healthy` | `degraded` | `unhealthy`
**Raises:** Never raises — returns `unhealthy` status with error detail instead.

---

#### `sync(cursor: Optional[str] = None, limit: int = 100) -> SyncResult`
Fetches a page of records from the external service and returns normalized documents.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `cursor` | `str` | No | `None` | Opaque pagination token from previous `SyncResult.cursor`. Pass `None` for full sync. |
| `limit` | `int` | No | `100` | Max records per page |

**Returns:** `SyncResult`
```json
{
  "status": "success",
  "documents": [
    {
      "id": "doc-id-123",
      "title": "Document title",
      "content": "Full text content",
      "metadata": { "author": "...", "created_at": "..." },
      "source_url": "https://service.com/item/123"
    }
  ],
  "cursor": "next-page-token-or-null",
  "total_synced": 100,
  "has_more": true,
  "sync_duration_ms": 1240
}
```
**Raises:** `SyncError` on unrecoverable errors. Transient errors trigger automatic retry.

---

#### Connector-Specific Methods
[List every method in `connector.py` that goes beyond the base interface, with full parameter and
return type documentation. For example:]

#### `get_user_profile() -> Dict[str, Any]`
[Description, parameters, returns, raises]

#### `send_message(to: str, subject: str, body: str) -> Dict[str, Any]`
[Description, parameters, returns, raises]

---

### 4.2 Data Models

#### NormalizedDocument
```json
{
  "id": "string — unique ID within the connector",
  "title": "string — human-readable title",
  "content": "string — full text for indexing",
  "content_type": "text/plain | text/html | application/json",
  "metadata": {
    "author": "string",
    "created_at": "ISO 8601 timestamp",
    "updated_at": "ISO 8601 timestamp",
    "tags": ["tag1", "tag2"],
    "source_url": "canonical URL"
  },
  "connector_id": "string",
  "tenant_id": "string",
  "synced_at": "ISO 8601 timestamp"
}
```

#### ConnectorHealth
```json
{
  "status": "healthy | degraded | unhealthy",
  "message": "human-readable status message",
  "latency_ms": 142,
  "last_checked": "ISO 8601 timestamp",
  "details": {}
}
```

#### AuthStatus
```json
{
  "authenticated": true,
  "user_email": "user@example.com",
  "expires_at": "ISO 8601 timestamp",
  "scopes": ["scope1", "scope2"],
  "token_type": "Bearer"
}
```

---

### 4.3 Error Reference

| Exception Class | HTTP Equiv | When Raised | Resolution |
|----------------|-----------|-------------|-----------|
| `ConfigurationError` | 400 | Missing required config key | Re-install with all required fields |
| `AuthenticationError` | 401 | Invalid/expired/revoked token | Re-authorize the connector |
| `PermissionError` | 403 | Missing OAuth scope | Re-authorize with correct scopes |
| `RateLimitError` | 429 | Provider rate limit exceeded | Auto-retried with `Retry-After` backoff |
| `ResourceNotFoundError` | 404 | Requested resource does not exist | Verify resource ID |
| `SyncError` | 500 | Unrecoverable sync failure | Check logs; retry sync |
| `ServiceUnavailableError` | 503 | Provider API is down | Wait and retry; monitor provider status |

---

### 4.4 Rate Limits
| Endpoint Category | Limit | Window | Backoff Strategy |
|------------------|-------|--------|-----------------|
| Read (sync) | [N] req/min | 60s | Exponential + Retry-After header |
| Write (send/update) | [N] req/min | 60s | Exponential + Retry-After header |
| Auth/Token | [N] req/min | 60s | Fixed 5s delay |

The connector automatically respects `Retry-After` response headers.
Max retry attempts: `settings.MAX_RETRIES` (default: 3).

---

## 5. Methods & Functionality Details

### 5.1 Method Inventory
Complete list of all methods implemented in `connector.py`:

| Method | Visibility | Async | Description |
|--------|-----------|-------|-------------|
| `initialize` | public | Yes | Configure connector with tenant config |
| `authorize` | public | Yes | OAuth2 exchange + token persistence |
| `health_check` | public | Yes | Verify API connectivity |
| `sync` | public | Yes | Paginated data sync |
| [custom method] | public | Yes | [Description] |
| `_refresh_token` | private | Yes | Auto-refresh expired access token |
| `_build_headers` | private | No | Build auth headers for API requests |
| `_normalize_record` | private | No | Transform raw API response to NormalizedDocument |

### 5.2 Data Flow
```
sync() called
    ↓
_refresh_token() if access_token near expiry
    ↓
_build_headers() → {"Authorization": "Bearer {token}"}
    ↓
HTTP GET {base_url}/api/endpoint?cursor={cursor}&limit={limit}
    ↓
[200 OK] → parse JSON → [_normalize_record() × N]
    ↓
Return SyncResult(documents=[...], cursor=next_token, has_more=True/False)

[429 Rate Limited] → wait Retry-After seconds → retry (max 3×)
[401 Unauthorized] → _refresh_token() → retry once → raise AuthenticationError
[5xx Server Error] → exponential backoff → retry (max 3×) → raise ServiceUnavailableError
```

### 5.3 Incremental Sync Strategy
[Describe exactly how the connector tracks what it has already synced:]
- Cursor type: [timestamp / opaque token / page number / offset]
- Cursor storage: `SyncResult.cursor` returned to caller; caller persists it
- Full sync trigger: `cursor=None` passed to `sync()`
- Max records per full sync pass: `limit` parameter (default 100)

### 5.4 Authentication Flow
```
First Install:
  User clicks Authorize → redirect to provider OAuth consent
  → provider redirects back with ?code=XXX
  → authorize(code=XXX) exchanges code for access_token + refresh_token
  → tokens stored in Redis via set_token()

Token Refresh:
  On every API call → check expires_at from Redis
  → if expires_at - now < 5 minutes → POST /token with refresh_token
  → store new access_token + updated expires_at
  → if refresh fails → raise AuthenticationError (user must re-authorize)
```

---

## 6. Testing Guide

### 6.1 Running Tests
```bash
# From shielva-connectors/ root:
cd generated_connectors/{tenant}/{connector_dir}
python3 -m pytest tests/ -v

# With coverage report:
python3 -m pytest tests/ -v --cov=. --cov-report=term-missing

# Run a single test class:
python3 -m pytest tests/test_connector.py::TestSync -v

# Run a single test:
python3 -m pytest tests/test_connector.py::TestSync::test_sync_empty_response -v
```

### 6.2 Test Matrix
Every test file MUST cover these scenarios:

| Scenario | Test Class | Test Method | Mocks Required |
|----------|-----------|-------------|---------------|
| Successful initialize | TestInitialize | test_initialize_success | None |
| Initialize missing required key | TestInitialize | test_initialize_missing_key | None |
| Successful authorize | TestAuthorize | test_authorize_success | HTTP POST /token |
| Authorize with invalid code | TestAuthorize | test_authorize_invalid_code | HTTP POST /token → 401 |
| Token refresh on expiry | TestAuthorize | test_token_auto_refresh | HTTP POST /token |
| Health check — healthy | TestHealthCheck | test_health_check_healthy | HTTP GET /profile |
| Health check — API down | TestHealthCheck | test_health_check_unhealthy | HTTP GET /profile → 503 |
| Health check — unauthorized | TestHealthCheck | test_health_check_unauthorized | HTTP GET /profile → 401 |
| Sync — first page | TestSync | test_sync_first_page | HTTP GET /data |
| Sync — with cursor | TestSync | test_sync_with_cursor | HTTP GET /data?cursor=X |
| Sync — empty response | TestSync | test_sync_empty_response | HTTP GET /data → [] |
| Sync — rate limited | TestSync | test_sync_rate_limited | HTTP GET /data → 429 |
| Sync — pagination (has_more) | TestSync | test_sync_has_more | HTTP GET /data |
| [Custom method] — success | Test[Method] | test_[method]_success | [relevant mocks] |
| [Custom method] — error | Test[Method] | test_[method]_error | [relevant mocks] |

### 6.3 Mocking Pattern
**Always patch where the name is USED, not where it is defined.**

```python
# connector.py does: from client.gmail_client import GmailClient
# CORRECT:
mocker.patch("connector.GmailClient")          # patches where it is imported/used

# WRONG:
mocker.patch("client.gmail_client.GmailClient")  # patches where it is defined — has no effect
```

### 6.4 Async Test Setup
All test methods that call async connector methods must be `async def` and decorated with
`@pytest.mark.asyncio` (or use `asyncio_mode=auto` in `pytest.ini`).

```python
# pytest.ini
[pytest]
asyncio_mode = auto

# test_connector.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.fixture
def connector(connector_config):
    from connector import MyConnector
    c = MyConnector()
    c.tenant_id = "test-tenant"
    c.connector_id = "test-connector"
    return c

@pytest.mark.asyncio
async def test_sync_success(connector, mocker):
    mock_client = mocker.patch("connector.ServiceClient")
    mock_client.return_value.list_items = AsyncMock(return_value={
        "items": [{"id": "1", "name": "Item 1"}],
        "nextPageToken": None,
    })
    result = await connector.sync()
    assert result.status == "success"
    assert len(result.documents) == 1
```

### 6.5 Test Coverage Requirements
| Area | Minimum Coverage | Target |
|------|-----------------|--------|
| `connector.py` — public methods | 90% | 100% |
| `connector.py` — private helpers | 70% | 85% |
| `exceptions.py` | 80% | 100% |
| Overall | 80% | 90% |

Coverage is measured by the CI pipeline after every test run.
A coverage drop below 80% fails the build.

### 6.6 Integration Test (Manual, Requires Real Credentials)
These are NOT automated — run manually before release:
1. Set `TEST_CLIENT_ID` and `TEST_CLIENT_SECRET` in your local `.env`
2. Run `pytest tests/integration/ -v -m integration`
3. Expected: all integration tests pass against live API
4. Verify in provider dashboard: test data created/updated/deleted as expected

---

## 7. Configuration Reference

### 7.1 Full Config Schema
```json
{
  "fields": [
    {
      "key": "client_id",
      "label": "OAuth Client ID",
      "type": "text",
      "required": true,
      "placeholder": "1234567890-abc.apps.googleusercontent.com",
      "help": "From the provider's developer console"
    },
    {
      "key": "client_secret",
      "label": "OAuth Client Secret",
      "type": "password",
      "required": true,
      "help": "Keep this secret — never share or commit"
    },
    {
      "key": "sync_interval",
      "label": "Sync Interval (seconds)",
      "type": "number",
      "required": false,
      "default": 3600,
      "min": 300,
      "max": 86400
    },
    {
      "key": "batch_size",
      "label": "Records Per Page",
      "type": "number",
      "required": false,
      "default": 100,
      "min": 1,
      "max": 500
    }
  ]
}
```

### 7.2 Tuning Guide
| Goal | Setting to Change | Recommended Value |
|------|------------------|------------------|
| Faster initial sync | `batch_size` | 500 (if provider allows) |
| Reduce API quota usage | `batch_size` | 25–50 |
| Near-real-time data | `sync_interval` | 300 (5 min) |
| Low-priority background sync | `sync_interval` | 86400 (24h) |

### 7.3 Multi-Tenant Isolation
Each connector instance is scoped to a single tenant.
- `tenant_id` comes from the auth context — never hardcoded
- Tokens stored in Redis under `connector:{tenant_id}:{connector_id}:tokens`
- Sync state stored in MongoDB scoped by `tenant_id`
- No cross-tenant data access is possible by design

---

## 8. Changelog

> Follows [Keep a Changelog](https://keepachangelog.com) format — newest first.
> Every code change that affects behaviour MUST be recorded here.

### [Unreleased]
#### Added
- [Describe new features not yet released]

#### Changed
- [Describe changed behaviour]

#### Fixed
- [Describe bug fixes]

---

### [1.0.0] — YYYY-MM-DD  _(Initial Release)_
#### Added
- Full connector implementation for [Provider] [Service]
- `initialize()` — tenant config validation and setup
- `authorize()` — OAuth2 authorization code exchange with token persistence
- `health_check()` — live API connectivity check with latency reporting
- `sync()` — paginated full and incremental sync with cursor support
- [custom method 1] — [description]
- [custom method 2] — [description]
- Automatic token refresh 5 minutes before expiry
- Exponential backoff on 429 / 5xx responses (max 3 retries)
- Structured logging via `structlog` throughout
- Unit test suite: [N] tests covering all public methods
- `exceptions.py` — typed exception hierarchy for all error scenarios
- `metadata/connector.json` — complete method + config schema

---

## 9. Release Notes

### v1.0.0 — Initial Release
**Released:** [YYYY-MM-DD]
**Compatibility:** Shielva Integration Builder >= 1.0, Python >= 3.9

**What's New:**
- First production-ready version of the [Provider] [Service] connector
- Supports full and incremental sync
- OAuth2 with automatic token refresh
- [N] connector-specific methods: [list them]

**Known Limitations:**
- Webhook/push delivery not yet supported (polling only)
- [Any other known limitation]

**Migration from Previous Version:**
- N/A (initial release)

**Breaking Changes:**
- None

---

## 10. Verification & Acceptance Criteria

### 10.1 Automated Checks (must all pass before merge)
```bash
# 1. Compile check — no syntax or import errors
python3 -m py_compile connector.py exceptions.py

# 2. Unit tests
python3 -m pytest tests/ -v

# 3. Coverage
python3 -m pytest tests/ --cov=. --cov-report=term-missing --cov-fail-under=80

# 4. Linting
ruff check connector.py exceptions.py

# 5. Metadata validation
python3 -c "import json; json.load(open('metadata/connector.json'))"
```

### 10.2 Manual Verification Steps
| Step | Action | Expected Result |
|------|--------|----------------|
| 1 | Install connector with valid credentials | Status: installed |
| 2 | Click "Authorize" | OAuth consent screen shown; redirect back to CMS |
| 3 | Click "Health Check" | `status: healthy`, latency < 2000ms |
| 4 | Click "Sync" (full) | Documents returned, cursor in response |
| 5 | Click "Sync" (incremental, same cursor) | Fewer or 0 new documents |
| 6 | Revoke token at provider | Next sync returns `AUTH_EXPIRED` |
| 7 | Re-authorize | New tokens stored, sync resumes |
| 8 | Simulate API outage (invalid base_url) | `SERVICE_UNAVAILABLE` error, no crash |

### 10.3 Performance Benchmarks
| Operation | Target Latency | Max Acceptable |
|-----------|---------------|---------------|
| `health_check()` | < 500ms | < 2000ms |
| `sync()` single page | < 3s | < 10s |
| `authorize()` | < 2s | < 5s |
| Token refresh | < 1s | < 3s |

---

## 11. Quality Checklist

### 11.1 Code Quality
- [ ] All methods have complete type hints (parameters + return type)
- [ ] Module, class, and every public method has a docstring
- [ ] No hardcoded secrets, tenant IDs, or environment-specific values
- [ ] All HTTP calls go through the connector's HTTP client (never raw `requests`)
- [ ] Credentials stored only via `self.set_token()` / `self.get_token()` → Redis
- [ ] Error handling uses typed exception classes from `exceptions.py`
- [ ] Structured logging via `structlog` — no `print()` statements
- [ ] No dead code, no commented-out blocks

### 11.2 Testing
- [ ] Unit tests for every public method (happy path + error path)
- [ ] All external HTTP calls are mocked — no real network calls in unit tests
- [ ] Mock patch paths use where-used pattern: `connector.ClientClass` not `client.module.ClientClass`
- [ ] Edge cases covered: empty response, last page, rate limit, auth expiry
- [ ] Async tests use `asyncio_mode=auto` or `@pytest.mark.asyncio`
- [ ] All tests pass: `pytest tests/ -v`
- [ ] Coverage >= 80%: `pytest tests/ --cov=. --cov-fail-under=80`

### 11.3 Security
- [ ] No secrets in source code or test files
- [ ] OAuth tokens never logged (not even at DEBUG level)
- [ ] API keys never appear in error messages or tracebacks
- [ ] HTTPS enforced for all external calls
- [ ] Input validation on all user-provided config parameters
- [ ] No `eval()`, `exec()`, or shell injection vectors

### 11.4 Resilience & Production Readiness
- [ ] `Retry-After` headers respected on 429 responses
- [ ] Exponential backoff on 5xx transient failures (max 3 retries)
- [ ] Request timeouts configured (default 60s, configurable)
- [ ] `health_check()` never raises — returns unhealthy status with detail
- [ ] `sync()` is resumable from a cursor — no data loss on interruption
- [ ] `sync()` handles empty response gracefully (returns `SyncResult` with `[]`)
- [ ] `sync()` handles pagination correctly (no duplicate or skipped records)
- [ ] `metadata/connector.json` is complete, valid JSON, and matches implementation

### 11.5 Documentation
- [ ] All sections of this template filled with connector-specific content
- [ ] Changelog updated for every code change
- [ ] API reference matches actual method signatures in `connector.py`
- [ ] Error reference lists all exception classes from `exceptions.py`
- [ ] Configuration schema matches `metadata/connector.json`
- [ ] Test matrix covers all methods

---

## 12. Troubleshooting

### 12.1 Common Error Reference
| Symptom | Most Likely Cause | Fix |
|---------|------------------|-----|
| `AuthenticationError` on every request | Refresh token revoked or expired | Re-authorize the connector in CMS |
| `RateLimitError` frequently | Batch size too large or sync too frequent | Reduce `batch_size`; increase `sync_interval` |
| Sync returns 0 documents | Wrong OAuth scopes | Re-authorize with correct scopes checked |
| `health_check` timeout | Network / firewall blocking outbound HTTPS | Verify outbound 443 to provider API host |
| `ConfigurationError: missing client_id` | Incomplete install form | Re-install with all required fields |
| Partial data on incremental sync | Cursor not persisted correctly | Check cursor is saved between sync calls |
| `ServiceUnavailableError` | Provider API is down | Monitor provider status page; retry later |
| Tokens missing after restart | Redis evicted keys | Increase Redis `maxmemory` or use persistence |

### 12.2 Debug Checklist
1. **Check connector logs**: CMS > Integrations > [Connector] > Activity Log
2. **Run health check**: quick connectivity and token validity test
3. **Check token expiry**: look for `expires_at` in connector metadata
4. **Verify scopes**: compare granted scopes vs required scopes in section 2.3
5. **Provider status**: check [provider status page URL]
6. **Network trace**: run `curl -v https://{provider-api-host}` from the connector host
7. **Redis check**: `redis-cli GET connector:{tenant}:{id}:tokens` — should not be empty

### 12.3 Log Interpretation
| Log Message | Meaning |
|-------------|---------|
| `connector.sync.started` | Sync job began — check `cursor` field |
| `connector.sync.completed` | Sync done — check `total_synced` and `has_more` |
| `connector.token.refreshed` | Access token was auto-refreshed |
| `connector.rate_limited` | Hit 429 — check `retry_after` field for wait time |
| `connector.auth_failed` | Token invalid/revoked — user must re-authorize |

---

## 13. FAQ

### General

**Q: How often does the connector sync?**
A: Sync is triggered on-demand from CMS or via the API. Automatic scheduled sync uses the
`sync_interval` config value (default: 3600 seconds). You can trigger an immediate sync at any time.

**Q: Is credential data encrypted?**
A: Yes. All tokens are stored in Redis via `BaseConnector.set_token()` which uses the platform's
encryption layer. Tokens are never written to disk or logged.

**Q: Can this connector run for multiple users/accounts in the same tenant?**
A: Each connector installation is its own instance with its own `connector_id`. You can install
the connector multiple times for different accounts within the same tenant.

**Q: What happens if my refresh token is revoked?**
A: The connector raises `AuthenticationError` on the next API call. You will see an error in CMS
and must click "Re-authorize" to complete a new OAuth flow.

### Technical

**Q: Why does the connector not use `requests` directly?**
A: All HTTP calls go through the shared HTTP client for consistent timeout handling, retry logic,
`Retry-After` header support, and structured request/response logging.

**Q: How does pagination work across multiple sync calls?**
A: `sync()` returns a `cursor` in `SyncResult`. The platform passes this cursor back on the next
`sync()` call. Passing `cursor=None` always starts a full sync from the beginning.

**Q: Can I add a new method to this connector?**
A: Yes — add the method to `connector.py`, add corresponding entry to `metadata/connector.json`,
write unit tests, and update the Method Inventory table in section 5.1.

**Q: Why does the mock need to patch `connector.ClientClass` instead of `client.module.ClientClass`?**
A: Python's mock library patches the name in the namespace where it is *used*. Since `connector.py`
does `from client.module import ClientClass`, the name `ClientClass` now lives in `connector`'s
namespace. Patching the original source has no effect on the already-imported reference.

---

## 14. Appendix

### 14.1 File Structure
```
{connector_dir}/
├── connector.py           # Main connector class (inherits BaseConnector)
├── exceptions.py          # Typed exception classes
├── client/
│   └── {service}_client.py  # Thin HTTP wrapper for external API
├── docs/
│   └── connector_docs.json  # Machine-readable version of this documentation
├── metadata/
│   └── connector.json     # Method signatures, config schema, capabilities
├── tests/
│   ├── conftest.py        # Fixtures and sys.path setup
│   └── test_connector.py  # Unit test suite
└── pytest.ini             # asyncio_mode = auto
```

### 14.2 Related Documentation
- [Shielva BaseConnector API](shared/base_connector.py)
- [Integration Builder Guide](docs/integration-builder.md)
- [Provider Official API Docs](https://developer.example.com)
- [OAuth2 RFC 6749](https://datatracker.ietf.org/doc/html/rfc6749)

### 14.3 Support & Contributing
- Bugs: raise an issue in the Shielva internal tracker
- Feature requests: `CMS > Support > Feature Request`
- Contributing: follow the connector coding standards in `CONTRIBUTING.md`
"""

# Sentinel string — updated to trigger auto-upgrade from v1 template (which lacked
# Changelog, full API reference, test matrix, etc.).
_DOC_GUIDELINES_SENTINEL = "Changelog — Comprehensive v2"


def _get_r2():
    """Lazy import r2_service to avoid circular imports."""
    from integration.services import r2_service

    return r2_service


async def _get_redis():
    """Lazy Redis connection."""
    import redis.asyncio as aioredis

    return await aioredis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True, health_check_interval=30, socket_keepalive=True, socket_connect_timeout=5)


# ── Version helpers ───────────────────────────────────────────────────


def _bump_version(current: str) -> str:
    """Increment the patch version: '1.0.0' -> '1.0.1'."""
    parts = current.split(".")
    if len(parts) == 3:
        parts[2] = str(int(parts[2]) + 1)
        return ".".join(parts)
    return "1.0.1"


# ── MongoDB helpers ───────────────────────────────────────────────────


def _doc_guidelines_collection():
    from integration.db.database import get_db

    return get_db()["connector_documentation_guidelines"]


# ── Public API ────────────────────────────────────────────────────────


async def get_active_doc_guidelines() -> dict[str, Any]:
    """Return active doc guidelines dict: {version, content, updated_at}.

    Cache hierarchy: Redis -> MongoDB (metadata) + R2 (content) -> default.
    """
    try:
        # 1. Get active version from MongoDB
        col = _doc_guidelines_collection()
        doc = await col.find_one({"is_active": True}, sort=[("created_at", -1)])
        if not doc:
            return _default_record()

        version = doc["version"]
        redis_key = _REDIS_KEY_TPL.format(version=version)

        # 2. Try Redis cache
        try:
            r = await _get_redis()
            cached = await r.get(redis_key)
            await r.aclose()
            if cached:
                logger.debug("doc_guidelines.cache_hit", version=version)
                return {
                    "version": version,
                    "content": cached,
                    "updated_at": str(doc.get("created_at", "")),
                }
        except Exception as exc:
            logger.warning("doc_guidelines.redis_error", error=str(exc))

        # 3. Read from R2 or local cache
        r2 = _get_r2()
        versioned_key = f"{_DOC_GUIDELINES_R2_PREFIX}/{_VERSIONED_KEY_TPL.format(version=version)}"
        content = await _r2_get_text(r2, versioned_key)

        if content:
            # Cache in Redis
            try:
                r = await _get_redis()
                await r.setex(redis_key, 3600, content)
                await r.aclose()
            except Exception:
                pass
            return {
                "version": version,
                "content": content,
                "updated_at": str(doc.get("created_at", "")),
            }

        # 4. Fallback: use content stored in MongoDB doc
        content = doc.get("content", DEFAULT_CONNECTOR_DOCUMENTATION_MD)
        return {
            "version": version,
            "content": content,
            "updated_at": str(doc.get("created_at", "")),
        }

    except Exception as exc:
        logger.error("doc_guidelines.get_failed", error=str(exc))
        return _default_record()


async def save_doc_guidelines(content: str, change_description: str = "") -> dict[str, Any]:
    """Save new version of doc guidelines to MongoDB + R2 + Redis.

    Deactivates previous active version, creates new one.
    Returns {version, content, updated_at}.
    """
    col = _doc_guidelines_collection()

    # Get current version to compute next
    prev = await col.find_one({"is_active": True}, sort=[("created_at", -1)])
    prev_version = prev["version"] if prev else "1.0.0"
    new_version = _bump_version(prev_version) if prev else "1.0.0"

    now = datetime.now(UTC)

    # Deactivate all current active
    await col.update_many({"is_active": True}, {"$set": {"is_active": False}})

    # Insert new version
    doc = {
        "version": new_version,
        "content": content,
        "change_description": change_description,
        "created_at": now,
        "is_active": True,
    }
    await col.insert_one(doc)

    # Save to R2
    r2 = _get_r2()
    versioned_key = f"{_DOC_GUIDELINES_R2_PREFIX}/{_VERSIONED_KEY_TPL.format(version=new_version)}"
    standard_key = f"{_DOC_GUIDELINES_R2_PREFIX}/{_STANDARD_KEY}"
    await _r2_put_text(r2, versioned_key, content)
    await _r2_put_text(r2, standard_key, content)  # keep standard up-to-date

    # Cache in Redis
    try:
        r = await _get_redis()
        redis_key = _REDIS_KEY_TPL.format(version=new_version)
        await r.setex(redis_key, 3600, content)
        await r.aclose()
    except Exception as exc:
        logger.warning("doc_guidelines.redis_cache_failed", error=str(exc))

    # Ingest into MCP RAG for semantic search across all connectors
    try:
        from integration.services.guidelines_service import _ingest_guidelines_to_rag

        await _ingest_guidelines_to_rag(content, f"Documentation Guidelines v{new_version}", "docs")
    except Exception as exc:
        logger.warning("doc_guidelines.rag_ingest_failed", error=str(exc))

    logger.info("doc_guidelines.saved", version=new_version)
    return {"version": new_version, "content": content, "updated_at": str(now)}


async def get_doc_guidelines_version_history() -> list[dict[str, Any]]:
    """Return all versions from MongoDB, newest first."""
    try:
        col = _doc_guidelines_collection()
        cursor = col.find({}, {"_id": 0, "content": 0}).sort("created_at", -1).limit(50)
        docs = await cursor.to_list(length=50)
        return [
            {
                "version": d.get("version"),
                "change_description": d.get("change_description", ""),
                "created_at": str(d.get("created_at", "")),
                "is_active": d.get("is_active", False),
            }
            for d in docs
        ]
    except Exception as exc:
        logger.error("doc_guidelines.history_failed", error=str(exc))
        return []


async def seed_default_doc_guidelines() -> None:
    """Seed / upgrade the connector_documentation.md on startup.

    Called from main.py lifespan.

    Behaviour:
    - First boot (no records): creates v1.0.0 in MongoDB + R2/local.
    - Subsequent boots: checks if the active record contains the Quality Checklist
      sentinel string.  If it is missing (older seed), auto-upgrades to a new version
      so the running instance always has up-to-date documentation guidelines.
    """
    try:
        col = _doc_guidelines_collection()
        active = await col.find_one({"is_active": True}, sort=[("created_at", -1)])

        if active:
            # Check whether the active version already has the comprehensive v2 content
            if _DOC_GUIDELINES_SENTINEL in active.get("content", ""):
                logger.info(
                    "doc_guidelines.seed_skipped",
                    reason="already_up_to_date",
                    version=active.get("version"),
                )
                return
            # Upgrade: create a new version with the comprehensive v2 template
            logger.info(
                "doc_guidelines.seed_upgrading",
                from_version=active.get("version"),
                reason="missing comprehensive v2 documentation guidelines",
            )
            await save_doc_guidelines(
                DEFAULT_CONNECTOR_DOCUMENTATION_MD,
                change_description=(
                    "Auto-upgrade to comprehensive v2: added Changelog, full API reference, "
                    "test matrix, mocking guide, performance benchmarks, release notes, "
                    "log interpretation, and complete configuration/tuning reference"
                ),
            )
            return

        # First boot — no records at all
        now = datetime.now(UTC)
        doc = {
            "version": "1.0.0",
            "content": DEFAULT_CONNECTOR_DOCUMENTATION_MD,
            "change_description": "Initial default — Shielva connector documentation standard",
            "created_at": now,
            "is_active": True,
        }
        await col.insert_one(doc)
        logger.info("doc_guidelines.seed_mongodb", version="1.0.0")

        # Write to R2 / local storage
        r2 = _get_r2()
        standard_key = f"{_DOC_GUIDELINES_R2_PREFIX}/{_STANDARD_KEY}"
        versioned_key = f"{_DOC_GUIDELINES_R2_PREFIX}/{_VERSIONED_KEY_TPL.format(version='1.0.0')}"
        await _r2_put_text(r2, standard_key, DEFAULT_CONNECTOR_DOCUMENTATION_MD)
        await _r2_put_text(r2, versioned_key, DEFAULT_CONNECTOR_DOCUMENTATION_MD)
        logger.info(
            "doc_guidelines.seed_r2",
            standard_key=standard_key,
            versioned_key=versioned_key,
        )

        # Ingest into MCP RAG on first boot
        try:
            from integration.services.guidelines_service import (
                _ingest_guidelines_to_rag,
            )

            await _ingest_guidelines_to_rag(
                DEFAULT_CONNECTOR_DOCUMENTATION_MD,
                "Documentation Guidelines v1.0.0",
                "docs",
            )
        except Exception as rag_exc:
            logger.warning("doc_guidelines.seed_rag_failed", error=str(rag_exc))

    except Exception as exc:
        logger.warning("doc_guidelines.seed_failed", error=str(exc))


# ── Internal helpers ─────────────────────────────────────────────────


def _default_record() -> dict[str, Any]:
    return {
        "version": "1.0.0",
        "content": DEFAULT_CONNECTOR_DOCUMENTATION_MD,
        "updated_at": "",
    }


async def _r2_get_text(r2, key: str) -> str | None:
    """Read text from R2 or local cache. Returns None if not found."""
    try:
        if r2._use_local():
            local_path = Path(r2._LOCAL_CACHE_DIR) / key
            if local_path.exists():
                return local_path.read_text(encoding="utf-8")
            return None
        loop = asyncio.get_event_loop()
        import boto3

        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name="auto",
        )
        get_fn = partial(s3.get_object, Bucket=settings.R2_SHARED_BUCKET, Key=key)  # shared admin bucket
        resp = await loop.run_in_executor(None, get_fn)
        return resp["Body"].read().decode("utf-8")
    except Exception:
        return None


async def _r2_put_text(r2, key: str, content: str) -> None:
    """Write text to R2 or local cache."""
    try:
        if r2._use_local():
            local_path = Path(r2._LOCAL_CACHE_DIR) / key
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(content, encoding="utf-8")
            return
        loop = asyncio.get_event_loop()
        import boto3

        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name="auto",
        )
        put_fn = partial(
            s3.put_object,
            Bucket=settings.R2_SHARED_BUCKET,  # shared admin bucket
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown",
        )
        await loop.run_in_executor(None, put_fn)
    except Exception as exc:
        logger.warning("doc_guidelines.r2_put_failed", key=key, error=str(exc))
