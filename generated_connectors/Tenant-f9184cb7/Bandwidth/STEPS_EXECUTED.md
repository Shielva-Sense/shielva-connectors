# Bandwidth Connector — Steps Executed (Audit Trail)

Built **as the Claude CLI behind SAD** — each step name maps to an actual SAD prompt loaded from `STEP_PROMPTS/{name}.txt` (R2 hot cache → Python constant fallback). The chain SAD takes is:

```
step_executor.py::handle_<step>            ← step entry point
  → _get_prompt(name, fallback)            ← r2_service.get_step_prompt
    → format(**context kwargs)             ← inject provider/service/auth_type/user_prompt/plan_constraints/...
      → call_llm_fix(messages=[user_msg], system=formatted, max_tokens=N)
        → llm_client._call_cli             ← runs `claude -p --output-format text --model $LLM_MODEL`
          → subprocess stdin = "=== SYSTEM INSTRUCTIONS ===\n{formatted}\n=== END ===\n\n{user_msg}"
```

Rubric: `connector_development_docs/12-canonical-build-steps.md`.

| # | StepType | SAD prompt | Artifact | Status |
|---|---|---|---|---|
| 0 | `generate_implementation_plan` | `_IMPL_PLAN_SYSTEM` (step_executor.py:4927) | [`implementation_plan.md`](implementation_plan.md) | ✅ |
| — | `(planning)` | `PLANNING_SYSTEM_PROMPT` (planning_prompt.py:137) | [`plan_steps.json`](plan_steps.json) | ✅ |
| 1 | `install_deps` | (config-driven) | [`requirements.txt`](requirements.txt) | ✅ |
| 2 | `write_connector` | `CONNECTOR_SYSTEM_PROMPT` (codegen_prompt.py:5) | [`connector.py`](connector.py) + 7 modules | ✅ |
| 3 | `smoke_test` | (in-process import + install()) | log captured below | ✅ |
| 4 | `write_tests` | `TEST_SYSTEM_PROMPT` (codegen_prompt.py:429) | [`tests/test_connector.py`](tests/test_connector.py) | ✅ 31/31 |
| 5 | `generate_metadata` | `_METADATA_SYSTEM_PROMPT` (step_executor.py:6131) | [`metadata/connector.json`](metadata/connector.json) | ✅ |

---

## Planning (PLANNING_SYSTEM_PROMPT) ✅

Substitutions used (matching `step_executor.py` write_connector context kwargs):

| Kwarg | Value |
|---|---|
| `provider` | `bandwidth` |
| `service_name` | `Communications` |
| `connector_name` | `Bandwidth` |
| `package_root` | `bandwidth` |
| `auth_type` | `basic` |
| `sdk_package` | `httpx` |
| `docs_url` | `https://dev.bandwidth.com` |
| `default_scopes` | `(none — Basic auth)` |
| `user_prompt` | "Build a Bandwidth CPaaS connector that covers Messaging (send/get/list SMS+MMS, media list/upload/delete), Voice (create/get/update/list calls, recordings list+download), and Numbers/Dashboard (list applications, list phone-number orders), plus webhook + signature verification handlers. Auth is HTTP Basic with account_id + API user." |
| `guidelines_version` | `(R2 STEP_PROMPTS/CODE_EXECUTION_GUIDELINES current)` |

Output → [`plan_steps.json`](plan_steps.json):

- `package_structure.root`: `bandwidth_connector`
- 13 files (`__init__.py`, `connector.py`, `config.py`, `models.py`, `exceptions.py`, `helpers/{__init__,utils,normalizer}.py`, `client/{__init__,http_client}.py`, `tests/{__init__,test_connector}.py`, `metadata/connector.json`)
- 8 recommended features (retry_logic, rate_limiting, pagination_handling, circuit_breaker, data_normalization, structured_logging, handle_webhook, process_callback)
- 6 default_config_fields (username, password, base_url, rate_limit_per_min, pagination_type, api_version) — auth-type "basic" per PLANNING_SYSTEM_PROMPT auth-type mapping
- 6 steps in mandated order: `generate_implementation_plan` → `install_deps` → `write_connector` → `smoke_test` → `write_tests` → `generate_metadata`

---

## Step 0 — `generate_implementation_plan` ✅

Artifact: [`implementation_plan.md`](implementation_plan.md) with the 9 mandated sections — service overview, auth, base URLs, endpoints catalogue, pagination, rate limits, **dependencies (§7 read by `install_deps`)**, error model, webhooks.

§7 lists only `tenacity>=8.2`.

---

## Step 1 — `install_deps` ✅

Reads §7 of implementation_plan.md.

Artifact: [`requirements.txt`](requirements.txt) → `tenacity>=8.2`.

Skipped (pre-installed): `pydantic`, `httpx`, `structlog`, `pytest`, `pytest-asyncio`, `pytest-mock`.

---

## Step 2 — `write_connector` (CONNECTOR_SYSTEM_PROMPT) ✅

System prompt loaded + formatted with these kwargs (matching `step_executor.py:1833-1845`):

```
base_connector_interface = BASE_CONNECTOR_INTERFACE         ← from planning_prompt.py:5
provider                 = "bandwidth"
service_name             = "Communications"
connector_name           = "Bandwidth"
auth_type                = "basic"
sdk_package              = "httpx"
docs_url                 = "https://dev.bandwidth.com"
default_scopes           = ""
user_prompt              = "<the Bandwidth prompt above>"
plan_constraints         = <rendered from plan_steps.json step #2 config>
step_memory_summary      = "implementation_plan.md (188 lines), requirements.txt (1 dep)"
```

User message:
> Generate the connector.py for this service. Return ONLY raw Python code — no markdown fences, no prose.

### CONNECTOR_SYSTEM_PROMPT rules satisfied

| Rule (line in codegen_prompt.py) | Applied to Bandwidth |
|---|---|
| Rule 1 — inherit from `BaseConnector` | ✅ `class BandwidthConnector(BaseConnector)` |
| Rule 2 — implement install/health_check/sync; `authorize()` only for oauth2_code/pkce | ✅ no `authorize()` override; `authorize()` is inherited as the base no-op |
| Rule 2 — `install()` MUST NOT call health_check or any API | ✅ verified by `test_install_with_creds_returns_healthy_without_api_call` (asserts `request.await_count == 0`) |
| Rule 4 — multi-tenant via `self.tenant_id` | ✅ NormalizedDocument id = `f"{tenant_id}_{source_id}"` |
| Rule 5 — never hardcode credentials | ✅ every credential via `self.config.get(...)`; AST scan clean |
| Rule 6 — `httpx.AsyncClient` for HTTP | ✅ in `client/http_client.py`; never in `connector.py` |
| Rule 7 — `structlog` MANDATORY | ✅ `import structlog; logger = structlog.get_logger(__name__)` in connector + http_client |
| Rule 8 — explicit HTTP status handling (401/403/429/timeout) | ✅ `_classify_failure` maps 401→TOKEN_EXPIRED+OFFLINE, 403→INVALID_CREDENTIALS+UNHEALTHY, 429→DEGRADED, timeout caught in http_client |
| Rule 12 — `AUTH_TYPE` from approved list | ✅ `AUTH_TYPE: str = "basic"` (not "basic_auth") |
| Import rule — `from shared.base_connector import (...)` with `shared.` prefix | ✅ no try/except fallback shim |
| Import rule — explicit typing imports | ✅ `from typing import Any, AsyncIterator, Dict, List, Optional, Union` |
| Enum values — only the 8 AuthStatus values | ✅ verified by AST + tests |
| `ConnectorStatus.connector_id` required positional | ✅ every construction passes `self.connector_id` |
| `SyncResult.documents_synced/_failed/_found` exact names | ✅ |
| `NormalizedDocument.id = f"{self.tenant_id}_{item_id}"` | ✅ (underscore, not colon) |
| Handler overrides use exact BaseConnector signatures | ✅ `handle_webhook`, `process_callback`, `handle_event`, `batch_processor` — all 4 |
| `hmac.compare_digest` for webhook signature | ✅ in `process_callback` |
| No `subprocess` / `eval` / `exec` / `__import__` | ✅ AST scan clean |

### Files written

| File | Role |
|---|---|
| `connector.py` | `BandwidthConnector` orchestrator |
| `client/http_client.py` | All httpx; Basic auth; retry + Retry-After + 5xx backoff |
| `helpers/normalizer.py` | `normalize_message`, `normalize_call` → `NormalizedDocument` (id = `tenant_id_source_id`) |
| `helpers/utils.py` | RFC 5988 Link parser, `pageToken` extractor, ISO date helper |
| `models.py` | Pydantic schemas (camelCase aliases) |
| `exceptions.py` | `BandwidthError` hierarchy |
| `config.py` | `BandwidthConfig` (pydantic-settings, env prefix `BANDWIDTH_`) — non-secret runtime knobs only |
| `__init__.py` | Package init |

---

## Step 3 — `smoke_test` ✅

Run with `PYTHONPATH=".:$SHIELVA_CORE"` so `shared.base_connector` resolves (same env SAD's pytest sees):

```
2026-06-21 15:56:46 [info     ] Connector initialized          connector_type=bandwidth tenant_id=t
2026-06-21 15:56:46 [warning  ] bandwidth.install.missing_credentials connector_id=smoke missing=['account_id','username','password'] tenant_id=t
smoke_test PASS offline missing_credentials
```

Import clean. `install()` with empty config returned `ConnectorStatus(OFFLINE, MISSING_CREDENTIALS)` without crashing. `BaseConnector` superclass init logged via structlog.

---

## Step 4 — `write_tests` (TEST_SYSTEM_PROMPT) ✅

System prompt formatted with `connector_code`, `class_name="BandwidthConnector"`, `provider`, `service_name`, `connector_name`, `auth_type="basic"`, `user_prompt`, `step_memory_summary`, `sdk_package="httpx"`.

### TEST_SYSTEM_PROMPT rules satisfied

| Rule | Applied |
|---|---|
| `from connector import BandwidthConnector` (rootdir-based, no package prefix) | ✅ |
| Patch targets begin with `connector.` (`connector.BandwidthHTTPClient`, `connector.logger`) | ✅ |
| `@pytest.fixture(autouse=True) def mock_logger():` with `patch("connector.logger")` | ✅ in conftest.py |
| httpx mock: `request = AsyncMock()`, response = `MagicMock()` (`.json()` is sync) | ✅ |
| `connector` fixture depends on `mock_BandwidthHTTPClient` so __init__ never instantiates real client | ✅ |
| Default list mock omits pagination token (avoid infinite loop) | ✅ — pagination tested via `side_effect` |
| `side_effect` uses plain dicts (no `AsyncMock(return_value=...)` wrappers) | ✅ |
| No `freezegun` / `factory_boy` / `hypothesis` / `faker` | ✅ |
| No `Z`-suffixed datetime strings (`fromisoformat` chokes <3.11) | ✅ — helpers/normalizer.py replaces `Z` → `+00:00` |
| Test asserts on normalised output (id format includes tenant_id) | ✅ `test_sync_aggregates_messages_and_calls` |

### Result

```
31 passed in 0.27s
```

Coverage:
- `TestInstall` (2) — missing creds, success without API call (rule 2 verification)
- `TestHealthCheck` (3) — missing, healthy probes /applications, failure → UNHEALTHY+FAILED
- `TestMessaging` (7) — send / get / list (paginated + no-next) / media list/upload/delete
- `TestVoice` (7) — create / get / update (with body + empty body) / list (paginated) / recordings list + download
- `TestDashboard` (2) — applications, phone-number orders
- `TestSync` (3) — missing creds, aggregation + tenant scoping, pagination via side_effect
- `TestCallbacks` (7) — process_callback (no secret / valid HMAC / invalid HMAC), handle_webhook (known route / ignored unknown), handle_event, batch_processor

---

## Step 5 — `generate_metadata` ✅

AST-extract methods + install_fields from `connector.py`, merge with plan's `install_fields`, write [`metadata/connector.json`](metadata/connector.json) with:

- `connector_type: "bandwidth"`
- `auth_type: "basic"` (matches `AUTH_TYPE` constant in connector.py)
- 5 `install_fields` (account_id, username, password, webhook_secret, timeout_s)
- 7 `default_config_fields` (all `bind: true` per PLANNING_SYSTEM_PROMPT default-bind rule)
- 22 methods catalogued with params + descriptions
- 16 capabilities, 7 engineering features

---

## How to re-run locally

The shared library is on PYTHONPATH inside SAD's venv. To reproduce the run here:

```bash
SHARED_ROOT="/Volumes/V3-SSD/Shielva Project Dirs/shielva-connectors/core"
cd ~/Documents/client_dir/bandwidth_connector
PYTHONPATH=".:$SHARED_ROOT" pytest tests/ -v
```

31 passed, 0 failed.
