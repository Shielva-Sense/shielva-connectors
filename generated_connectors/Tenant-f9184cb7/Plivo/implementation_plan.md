# Plivo Connector — Implementation Plan

## Goal

Ship a Shielva connector that wraps the Plivo REST API surface for voice + SMS,
matching the structural conventions of the Gmail connector while staying
provider-appropriate (HTTP Basic auth, action-oriented — no document sync).

## Architecture

```
plivo_connector/
├── __init__.py
├── connector.py              # PlivoConnector — orchestration only
├── exceptions.py             # PlivoError, PlivoAuthError, PlivoNetworkError, PlivoNotFound, PlivoRateLimitError
├── models.py                 # Local dataclasses + @property shims
├── client/
│   ├── __init__.py           # re-exports PlivoHTTPClient
│   └── http_client.py        # httpx async; HTTP Basic; retry on 429/5xx
├── helpers/
│   ├── __init__.py
│   └── utils.py              # with_retry, compact_params, normalize_e164
├── metadata/connector.json   # 10 top-level keys, install_fields, apis[]
├── instructions/setup.md
├── tests/
│   ├── __init__.py
│   ├── conftest.py           # mocks BaseConnector storage; supplies test config
│   └── test_connector.py     # respx-mocked; 14+ tests
├── conftest.py               # adds ROOT to sys.path
├── pytest.ini
├── requirements.txt
├── plan_steps.json           # TOP-LEVEL ARRAY of step dicts
└── stepper_progress.json
```

## Key invariants

1. `from shared.base_connector import BaseConnector` — the only legal way to
   reach the platform contract. Local mirror types live in `models.py`.
2. `PlivoConnector.CONNECTOR_TYPE = "plivo"`, `PlivoConnector.AUTH_TYPE = "api_key"` —
   class attributes, not instance.
3. Sibling imports are absolute (`from client.http_client import …`, never
   `from .http_client import …`).
4. The HTTP client never touches business logic. Connector methods compose
   parameters and delegate.
5. Retry is layered: the HTTP client retries 429/5xx inside one call; the
   connector wraps each call in `with_retry()` for transient network blips.
6. No OAuth token plumbing — `on_token_refresh` returns a synthetic
   `TokenInfo` so the platform contract is satisfied, but the real auth is the
   Basic header built fresh on every request.
7. `sync()` returns `SyncStatus.COMPLETED` with zero documents — Plivo is an
   action connector, not a knowledge-base source.

## Endpoint map

| Method | Endpoint |
|--------|----------|
| `get_account` | `GET /Account/{auth_id}/` |
| `send_sms` | `POST /Account/{auth_id}/Message/` |
| `get_message` | `GET /Account/{auth_id}/Message/{uuid}/` |
| `list_messages` | `GET /Account/{auth_id}/Message/` |
| `make_call` | `POST /Account/{auth_id}/Call/` |
| `get_call` | `GET /Account/{auth_id}/Call/{uuid}/` |
| `list_calls` | `GET /Account/{auth_id}/Call/` |
| `hangup_call` | `DELETE /Account/{auth_id}/Call/{uuid}/` |
| `transfer_call` | `POST /Account/{auth_id}/Call/{uuid}/` |
| `list_numbers` | `GET /Account/{auth_id}/Number/` |
| `search_phone_numbers` | `GET /PhoneNumber/?country_iso=…` |
| `buy_phone_number` | `POST /PhoneNumber/{number}/` |
| `list_applications` | `GET /Account/{auth_id}/Application/` |
| `create_application` | `POST /Account/{auth_id}/Application/` |

## Test strategy

Every connector method has at least one respx-mocked test. Specific assertions:

- Basic-auth header equals `Basic base64(auth_id:auth_token)`.
- `send_sms` POST body contains the exact Plivo field names (`src`, `dst`,
  `text`, `type`, `method`, `log`, `trackable`) and omits optional keys when
  unset.
- `make_call` POST body carries `from`, `to`, `answer_url`, `answer_method`,
  `time_limit`, `machine_detection`.
- `list_messages` / `list_calls` / `list_numbers` build the query string from
  caller filters and omit `None` values.
- `search_phone_numbers` hits `/PhoneNumber/` at the root (not `/Account/`).
- 429 responses are retried by the HTTP client and eventually succeed.

`asyncio.sleep` is monkey-patched to a no-op in the retry test so the suite
stays fast.
