# RudderStack Connector — Implementation Plan

## 1. Overview

The **RudderStack** connector (`RudderstackConnector`, `CONNECTOR_TYPE = "rudderstack"`,
`AUTH_TYPE = "api_key"`) wraps the two distinct surfaces RudderStack exposes:

| Surface | Base URL | Auth | Purpose |
|---|---|---|---|
| **Data Plane** (HTTP API) | `https://hosted.rudderlabs.com` | HTTP **Basic** — `write_key` as username, empty password | Event ingestion — `track` / `identify` / `page` / `group` / `screen` / `alias` / `batch` |
| **Control Plane** (Management API v2) | `https://api.rudderstack.com/v2` | **Bearer** `<personal_access_token>` | Read / write **Sources**, **Destinations**, **Connections**, **Workspaces**, **Profiles**, **Identities** |

The two-surface split is the most important architectural decision: a single
connector class exposes both, but the HTTP client picks the base URL + auth
header per request (`kind="data"` vs `kind="control"`). The data plane requires
only `write_key`; the control plane requires a `access_token` (PAT). Both are
install-time secrets — never hardcoded.

Capabilities:

- Event ingestion: `track_event`, `identify_user`, `page_event`, `screen_event`,
  `group_event`, `alias_user`, `batch_events`
- Sources: `list_sources`, `get_source`, `create_source`
- Destinations: `list_destinations`, `get_destination`
- Connections: `list_connections`
- Workspace: `list_workspaces`
- Profiles / Identities (Profiles API): `list_profiles`, `get_profile`, `list_identities`
- Lifecycle: `install`, `health_check`, `sync` (no-op — event streaming, no corpus)

## 2. SDK / Package Selection

| Package | Why |
|---|---|
| `httpx>=0.27` (already in shared core) | Single async HTTP client supporting Basic + Bearer + retries |
| `structlog>=24` (already in shared core) | Structured logging — every retry / error correlation field |
| No RudderStack SDK | The official `rudder-sdk-python` is sync (`requests`) and only covers the data plane. We need control-plane coverage and async I/O — direct `httpx` calls are smaller and faster |

Connector-only requirements:

```text
httpx>=0.27.0
pytest>=7
pytest-asyncio>=0.23
respx>=0.21
pytest-mock>=3.12
```

## 3. Auth Flow

API-key + bearer-token connector — no OAuth code exchange, no token refresh.

| Phase | Behaviour |
|---|---|
| `install()` | Validate `write_key` is present (required for data plane). PAT (`access_token`) is optional — if blank, control-plane methods raise `RudderstackAuthError`. Persists config via `save_config()`. Does **not** call any RudderStack endpoint. |
| `authorize()` | No-op — returns a `TokenInfo` whose `access_token = write_key` for interface symmetry with OAuth connectors. |
| `health_check()` | If PAT present → `GET /sources?limit=1`. If only `write_key` → minimal data-plane validation (the data plane has no GET probe — install presence is the only signal). 2xx → HEALTHY/CONNECTED; classified via `_STATUS_MAP` otherwise. |

### Headers per surface

```
# Control plane
Authorization: Bearer <access_token>
Content-Type: application/json
Accept: application/json

# Data plane
Authorization: Basic <base64(write_key + ":")>
Content-Type: application/json
```

The `write_key` is sent as the HTTP Basic username with an **empty password** —
the canonical Segment-compatible auth scheme RudderStack adopted.

## 4. Data Model

RudderStack is an **event-streaming** CDP, not a document corpus. There is no
"document sync" — events leave the system the moment they're ingested. We still
provide a `NormalizedDocument` adapter for the control-plane resources so that
KB ingest is symmetric with other connectors:

| Resource | NormalizedDocument.id | title | content | metadata |
|---|---|---|---|---|
| Source | `f"{tenant_id}_{source_id}"` | `name` | `f"{type} source"` | `type`, `enabled`, `writeKey`, `kind="rudderstack.source"` |
| Destination | `f"{tenant_id}_{destination_id}"` | `name` | `f"{type} destination"` | `type`, `enabled`, `kind="rudderstack.destination"` |

`sync()` itself remains a no-op for the event surface — returning
`SyncResult(status=COMPLETED, message="RudderStack is event-streaming")` —
because pulling the full event log is not a CDP-supported operation.

## 5. Key API Endpoints & Methods

### Data plane (HTTP Basic)

| Method | Endpoint | Body |
|---|---|---|
| `track_event(user_id, event, properties=None, write_key=None, timestamp=None)` | `POST /v1/track` | `{userId, event, properties, timestamp, sentAt}` |
| `identify_user(user_id, traits=None, write_key=None, timestamp=None)` | `POST /v1/identify` | `{userId, traits, timestamp, sentAt}` |
| `page_event(user_id, name=None, properties=None, write_key=None, timestamp=None)` | `POST /v1/page` | `{userId, name, properties, timestamp, sentAt}` |
| `screen_event(user_id, name=None, properties=None, write_key=None, timestamp=None)` | `POST /v1/screen` | `{userId, name, properties, timestamp, sentAt}` |
| `group_event(user_id, group_id, traits=None, write_key=None, timestamp=None)` | `POST /v1/group` | `{userId, groupId, traits, timestamp, sentAt}` |
| `alias_user(user_id, previous_id, write_key=None, timestamp=None)` | `POST /v1/alias` | `{userId, previousId, timestamp, sentAt}` |
| `batch_events(events, write_key=None)` | `POST /v1/batch` | `{batch: [...events], sentAt}` |

Each call returns the parsed body (`{"status": "OK"}` on success).

### Control plane (Bearer)

| Method | Endpoint |
|---|---|
| `list_sources(limit=50, after=None)` | `GET /sources` |
| `get_source(source_id)` | `GET /sources/{id}` |
| `create_source(name, type, config=None)` | `POST /sources` |
| `list_destinations(limit=50, after=None)` | `GET /destinations` |
| `get_destination(destination_id)` | `GET /destinations/{id}` |
| `list_connections()` | `GET /connections` |
| `list_workspaces()` | `GET /workspaces` |
| `list_profiles(limit=50, after=None)` | `GET /profiles` |
| `get_profile(profile_id)` | `GET /profiles/{id}` |
| `list_identities(limit=50, after=None)` | `GET /identities` |

## 6. Error Handling

`exceptions.py`:

```
RudderstackError                      # base, carries status_code + response_body
├── RudderstackAuthError              # 401 / 403
├── RudderstackBadRequestError        # 400
├── RudderstackNotFoundError          # 404
├── RudderstackConflictError          # 409
├── RudderstackRateLimitError         # 429 (retry_after_s)
└── RudderstackServerError            # 5xx
```

Back-compat aliases preserved: `RudderstackNetworkError = RudderstackServerError`,
`RudderstackNotFound = RudderstackNotFoundError`.

| Status | HTTP client behaviour |
|---|---|
| 400 / 401 / 403 / 404 / 409 | Raise immediately |
| 429 | Exponential backoff (1s, 2s, 4s + jitter), up to 3 retries |
| 5xx | Same exponential backoff |
| `httpx.TimeoutException` / `NetworkError` | Backoff retry, then `RudderstackNetworkError` |

`_STATUS_MAP` on the connector class drives health classification:

```
401 → (OFFLINE,   TOKEN_EXPIRED)
403 → (UNHEALTHY, INVALID_CREDENTIALS)
429 → (DEGRADED,  CONNECTED)
```

## 7. Dependencies

```bash
pip install httpx>=0.27 pytest>=7 pytest-asyncio>=0.23 respx>=0.21 pytest-mock>=3.12
```

`structlog` is provided by the shielva-connectors core lib.

## 8. Config & Install Fields

| Key | Type | Required | Source | Purpose |
|---|---|---|---|---|
| `write_key` | secret | **yes** | install_field | HTTP Basic username for data plane |
| `access_token` | secret | no | install_field | PAT — Bearer for control plane |
| `data_plane_url` | string | no | install_field (default `https://hosted.rudderlabs.com`) | Data plane base URL |
| `control_plane_url` | string | no | install_field (default `https://api.rudderstack.com/v2`) | Control plane base URL |
| `rate_limit_per_min` | number | no | install_field (default 100) | Soft client-side cap |

Class constants (never user-supplied):

```python
REQUIRED_CONFIG_KEYS = ["write_key"]
_DATA_PLANE_BASE     = "https://hosted.rudderlabs.com"
_CONTROL_PLANE_BASE  = "https://api.rudderstack.com/v2"
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility |
|---|---|
| `connector.py` | Orchestration only. Implements `install`, `authorize`, `health_check`, `sync`, plus one `async def` per user-facing operation. No raw HTTP, no JSON parsing, no retry logic. |
| `client/http_client.py` | Single owner of HTTP. Builds Bearer + Basic headers; routes per `kind`; retries 429 / 5xx with exponential backoff + jitter; raises typed exceptions. |
| `helpers/normalizer.py` | Maps control-plane resources (source, destination) into `NormalizedDocument`. |
| `helpers/utils.py` | `iso8601_now`, `normalize_event_payload`, `with_retry` (top-level async retry for non-HTTP transient errors). |
| `exceptions.py` | Full exception hierarchy + back-compat aliases. |
| `models.py` | Pydantic-style dataclasses describing API shapes (sources, destinations, events) for callers that want typed structures. |
| `metadata/connector.json` | `apis` catalogue + `install_fields`. The gateway loads this for the install form and the Test-APIs UI. |
| `.shielva/docs/connector_docs.json` | 7-section connector documentation surfaced in ACP. |
| `tests/` | `respx`-mocked unit tests — zero real I/O. |
