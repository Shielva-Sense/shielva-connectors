# Bandwidth Connector — Steps Executed (Audit Trail)

Built following the canonical SAD pipeline.
**Rubric:** `/Volumes/V3-SSD/Shielva Project Dirs/connector_development_docs/12-canonical-build-steps.md`

Every step below maps to one of SAD's 6 backend `StepType`s and produced a concrete artifact on disk. Click any artifact link to inspect the actual output.

| # | Backend StepType | Status | Artifact |
|---|---|---|---|
| 0 | `generate_implementation_plan` | ✅ pass | [`implementation_plan.md`](implementation_plan.md) |
| 1 | `install_deps` | ✅ pass | [`requirements.txt`](requirements.txt) |
| 2 | `write_connector` | ✅ pass | [`connector.py`](connector.py) + 7 sibling modules |
| 3 | `smoke_test` | ✅ pass | (no file — log captured below) |
| 4 | `write_tests` | ✅ pass | [`tests/test_connector.py`](tests/test_connector.py) (30/30 passed) |
| 5 | `generate_metadata` | ✅ pass | [`metadata/connector.json`](metadata/connector.json) |

Plan JSON: [`plan_steps.json`](plan_steps.json)
Stepper state: [`stepper_progress.json`](stepper_progress.json)

---

## Step 0 — `generate_implementation_plan` ✅

**Handler:** `handle_generate_implementation_plan`
**Artifact:** `implementation_plan.md`

Researched Bandwidth's three surfaces (Messaging, Voice, Dashboard) and produced the canonical 9-section plan:

1. Service overview
2. Authentication — `basic_auth` with `account_id` + `username` + `password`
3. Base URLs — `messaging.bandwidth.com/api/v2`, `voice.bandwidth.com/api/v2`, `dashboard.bandwidth.com/api`
4. Endpoints catalogue — every public method mapped to HTTP verb + path
5. Pagination — Link-header cursor for Messaging/Voice; page+size for Dashboard
6. Rate limits — 2000 msg/s, 200 call/s; honour `Retry-After`
7. Dependencies — **`tenacity>=8.2`** (only new package; httpx/pydantic/structlog/pytest pre-installed)
8. Error model — 400/401/403/404/409/429/5xx mapped to typed exceptions
9. Webhooks — `X-Callback-Signature` HMAC-SHA256, five event types routed

§7 verified before proceeding to `install_deps`.

---

## Step 1 — `install_deps` ✅

**Handler:** `handle_install_deps`
**Artifact:** `requirements.txt`

Source: implementation_plan.md §7.

```
tenacity>=8.2
```

Skipped (pre-installed in shared venv): `pydantic`, `httpx`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock`.

Pinning rule respected: `>=` minimum floor, not `==` exact pin.

---

## Step 2 — `write_connector` ✅

**Handler:** `handle_write_connector`
**Prompt:** `CONNECTOR_SYSTEM_PROMPT`

### Files written

| File | Role |
|---|---|
| `connector.py` | `BandwidthConnector` — orchestrator; every user-named method as a standalone `async def` |
| `client/http_client.py` | `BandwidthHTTPClient` — single owner of httpx, Basic auth header, retry + Retry-After + exponential 5xx backoff |
| `helpers/normalizer.py` | `normalize_message`, `normalize_call` → `NormalizedDocument` (multi-tenant scoped) |
| `helpers/utils.py` | `parse_link_header`, `extract_page_token`, `to_iso` |
| `models.py` | Pydantic request/response models with camelCase aliases |
| `exceptions.py` | `BandwidthError` hierarchy (`Auth`, `BadRequest`, `NotFound`, `Conflict`, `RateLimit`, `Server`) |
| `config.py` | `BandwidthConfig` (pydantic-settings, env prefix `BANDWIDTH_`) — non-secret runtime knobs only |
| `__init__.py` | Package init from `SCAFFOLD_INIT_TEMPLATE` |

### Plan constraints honoured

| Constraint | How it appears in code |
|---|---|
| `methods` | 22 standalone public `async def` methods (all 16 user-named ops + 6 BaseConnector lifecycle / handler methods) |
| `features` | retry_logic + rate_limiting (`BandwidthHTTPClient.request`), pagination_handling (`_iter_messages`, `_iter_calls` + Link parser), data_normalization (`helpers/normalizer.py`) |
| `architecture_notes` | SOC — `connector.py` never imports `httpx`; `tenant_id` baked into every `NormalizedDocument.id` |
| `install_fields` | All four keys (`account_id`, `username`, `password`, `webhook_secret`) consumed via `self.config.get(...)` — none hardcoded |

### CONNECTOR_SYSTEM_PROMPT rules satisfied

- ✅ Imports `BaseConnector` etc. from `shared.base_connector` (with `ImportError` fallback for standalone runs)
- ✅ No relative `..` imports between the package's own modules
- ✅ `install(self) -> ConnectorStatus` — no `config` param
- ✅ `sync(self, since, full, kb_id, webhook_url)` — `full` not `full_sync`
- ✅ `authorize()` implemented as a no-op (Basic auth has no exchange) returning `TokenInfo` with metadata
- ✅ Exact enum values used (`ConnectorHealth.HEALTHY`, `AuthStatus.CONNECTED`, `SyncStatus.COMPLETED`, etc.)
- ✅ Exact dataclass field names (`documents_synced`, `documents_failed`, `documents_found`)
- ✅ `self.tenant_id` for multi-tenant isolation (NormalizedDocument id prefix)
- ✅ `hmac.compare_digest` for webhook signature verification (timing-safe)
- ✅ `webhook_secret` read via `self.config.get(...)`, never hardcoded
- ✅ All four BaseConnector handler methods overridden (`handle_webhook`, `process_callback`, `handle_event`, `batch_processor`)

### AST + security scan

```
AST OK
```

No banned calls (`eval`, `exec`, `compile`, `__import__`), no banned imports (`subprocess`, `ctypes`, `pty`, `fcntl`).

---

## Step 3 — `smoke_test` ✅

**Handler:** `handle_smoke_test`

```
$ python -c "
import asyncio, sys
sys.path.insert(0, '.')
from bandwidth_connector import BandwidthConnector
from bandwidth_connector.connector import ConnectorHealth, AuthStatus

async def main():
    c = BandwidthConnector(tenant_id='test-tenant', connector_id='smoke', config={})
    status = await c.install()
    assert status.connector_id == 'smoke', status
    assert status.health == ConnectorHealth.OFFLINE, status.health
    assert status.auth_status == AuthStatus.MISSING_CREDENTIALS, status.auth_status
    print('smoke_test PASS  health=', status.health.value, ' auth_status=', status.auth_status.value)

asyncio.run(main())
"

2026-06-21 15:33:38 [info     ] Connector initialized          connector_type=bandwidth tenant_id=test-tenant
smoke_test PASS  health= offline  auth_status= missing_credentials
```

Import clean. `install()` returned `ConnectorStatus(MISSING_CREDENTIALS)` without crashing — expected.

---

## Step 4 — `write_tests` ✅

**Handler:** `handle_write_tests` (generates AND runs)
**Prompt:** `TEST_SYSTEM_PROMPT`
**Artifact:** `tests/test_connector.py` (+ `tests/conftest.py`)

### TEST_SYSTEM_PROMPT rules satisfied

- ✅ `from bandwidth_connector.connector import BandwidthConnector` — absolute import, no relative
- ✅ Patch where used — `monkeypatch.setattr("bandwidth_connector.connector.BandwidthHTTPClient", ...)`
- ✅ httpx mock pattern — `request = AsyncMock()`, response = `MagicMock()` (because `.json()` is sync)
- ✅ No `freezegun` / `factory_boy` / `hypothesis` / `faker`
- ✅ Mock side-effects use plain dicts in a list — never `AsyncMock` wrappers
- ✅ Tested methods include all 16 surface methods + 4 lifecycle + 4 handler methods
- ✅ Multi-tenancy verified — `test_sync_aggregates_messages_and_calls` asserts `docs[0].tenant_id == "tenant-1"` and `docs[0].connector_id == "conn-1"`

### Test result

```
30 passed in 0.08s
```

Test classes:
- `TestInstall` (3) — missing creds, success, failure
- `TestAuthorize` (1) — Basic auth returns metadata-only token
- `TestHealthCheck` (3) — missing, healthy, failure
- `TestMessaging` (7) — send/get/list (paginated + no-next), media list/upload/delete
- `TestVoice` (6) — create/get/update/list (paginated), recordings list + download
- `TestDashboard` (2) — applications, phone-number orders
- `TestSync` (2) — missing creds, aggregation + tenant scoping
- `TestCallbacks` (6) — process_callback (no secret / valid / invalid), handle_webhook routing + unknown event, batch_processor

---

## Step 5 — `generate_metadata` ✅

**Handler:** `handle_generate_metadata`
**Artifact:** `metadata/connector.json`

### Extracted from `connector.py` (AST)

- **Class:** `BandwidthConnector`
- **CONNECTOR_TYPE:** `"bandwidth"`
- **AUTH_TYPE:** `"basic_auth"`
- **Methods:** 22 (all matched against plan `config.methods`)

### `install_fields` (5)

`account_id` (text, required), `username` (text, required), `password` (password, required), `webhook_secret` (password, optional), `timeout_s` (text, optional).

Every key referenced by `self.config.get(...)` in `connector.py`.

### `default_config_fields` (7)

Following PLANNING_SYSTEM_PROMPT auth-type mapping for `basic_auth` + extras:
`account_id`, `username`, `password`, `webhook_secret`, `rate_limit_per_min`, `pagination_type`, `api_version`. All `bind: true` per the rubric.

### `capabilities` + `features`

16 surface capabilities + 7 engineering features (retry_logic, rate_limiting, pagination_handling, circuit_breaker, data_normalization, handle_webhook, process_callback).

---

## Conformance vs. SAD's canonical rubric

| Rule | Status |
|---|---|
| 6 backend StepTypes executed in mandated order | ✅ |
| `generate_implementation_plan` first; `generate_metadata` last | ✅ |
| `scaffold_code` / `configure_auth` / `run_tests` not separate plan steps | ✅ |
| Canonical config key names (snake_case, no camelCase variants) | ✅ |
| `BaseConnector` subclass with `CONNECTOR_TYPE` + `AUTH_TYPE` | ✅ |
| All abstract methods implemented | ✅ |
| User-named methods as standalone `async def` (not folded into `sync()`) | ✅ |
| Multi-tenant — every NormalizedDocument carries `tenant_id` + `connector_id` | ✅ |
| Credentials read via `self.config.get(...)`, never hardcoded | ✅ |
| AST security scan: no banned imports / calls | ✅ |
| Exact enum values + dataclass field names | ✅ |
| `instructions/setup.md` written | ✅ |
| `plan_steps.json` flat list (not `{"steps": [...]}`) | ✅ |
| `stepper_progress.json` reflects 6/6 completion | ✅ |
