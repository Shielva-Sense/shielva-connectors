# Keap Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Keap** (formerly Infusionsoft) is a small-business CRM + marketing-automation platform exposing a REST API under `https://api.infusionsoft.com/crm/rest/v1` (v1) and `/crm/rest/v2` (v2). This connector — `KeapConnector` (`CONNECTOR_TYPE = "keap"`, `AUTH_TYPE = "oauth2_code"`) — wraps the operational surfaces a Shielva tenant typically needs from a Keap account:

| Surface | Base path | Capability |
|---|---|---|
| Contacts | `/contacts` | List / get / create / update / delete contacts, filter by email/name |
| Opportunities | `/opportunities` | List / create sales opportunities |
| Orders | `/orders` | List e-commerce orders |
| Products | `/products` | List products (v2 catalogue) |
| Tags | `/tags` | List tags, apply / remove on contacts |
| Companies | `/companies` | List / get companies |
| Notes | `/notes` | List / create contact notes |
| Tasks | `/tasks` | List / create / complete tasks |
| Emails | `/emails/queue` | Send transactional emails |
| Affiliates | `/affiliates` | List affiliates |
| Campaigns | `/campaigns` | List campaigns, enroll contacts in sequences |

The connector normalises contacts + opportunities + orders into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), surfaces standalone `async def` methods per user-requested operation (OCP), and refreshes OAuth2 access tokens transparently on `401` via a one-shot HTTP-client callback.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request/response schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `PyJWT` | `>=2.8,<3.0` | Verifying signed Keap webhook envelopes (future) |
| `tenacity` | `>=8.2` | Retry decorator for `KeapRateLimitError`-style 429 handling |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`, `httpx`.

## 3. Auth Flow

Keap uses **OAuth 2.0 Authorization Code grant** with mandatory refresh tokens. Access tokens expire after ~24 h; refresh tokens are single-use and must be persisted as the connector receives them.

### Credentials
- `client_id` — OAuth app Client ID from Keap → Developer Portal → application. install_field (type `string`, required).
- `client_secret` — OAuth app Client Secret. install_field (type `secret`, required).
- `redirect_uri` — registered callback URL. install_field (type `string`, optional — gateway injects at deploy time when blank).
- `scopes` — space-separated; Keap exposes only `"full"` for REST v1. install_field (default `"full"`).
- `authorization_url` — `https://accounts.infusionsoft.com/app/oauth/authorize` (default; install_field overridable).
- `token_url` — `https://api.infusionsoft.com/token` (default; install_field overridable).
- `base_url` — `https://api.infusionsoft.com/crm/rest/v1` (default; install_field overridable for v2 testing).
- `rate_limit_per_min` — `60` (Keap-published throttle).

### Header contract
Every request to `https://api.infusionsoft.com/crm/rest/v1/*`:

```
Authorization: Bearer <access_token>
Content-Type:  application/json
Accept:        application/json
```

### Lifecycle
- `install()` validates `client_id` + `client_secret` are non-empty. Persists them via `save_config`. Does **not** call the API.
- `authorize(auth_code, state)` — POSTs `grant_type=authorization_code` to `token_url`; returns `TokenInfo`, persists via `set_token`.
- `on_token_refresh()` — POSTs `grant_type=refresh_token` to `token_url` with the stored refresh token; returns the new `TokenInfo`.
- `_refresh_access_token()` — connector-side adapter handed to `KeapHTTPClient` so a 401 triggers a one-shot refresh + retry without leaking auth concerns into the HTTP layer.
- `health_check()` — `GET /account/profile`; classifies 200 / 401 / 5xx via `_STATUS_MAP`.
- `ensure_token()` — inherited from `BaseConnector`; checks `is_token_valid()` (tz-aware compare) and refreshes when expired.

## 4. Data Model

### 4.1 Contact → NormalizedDocument

| NormalizedDocument | Keap JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{contact['id']}"` | tenant-scoped |
| `source_id` | `str(contact["id"])` | Keap contact ID |
| `title` | `"{given_name} {family_name} <{email}>"` | |
| `content` | name + email + company | |
| `source` | `"keap.contacts"` | |
| `metadata` | `{email, given_name, family_name, company, tag_ids}` | |

### 4.2 Opportunity → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{opp['id']}"` |
| `source_id` | `opp["id"]` |
| `title` | `opp["opportunity_title"]` |
| `content` | `"{title} — stage: {stage}, projected: {revenue}"` |
| `source` | `"keap.opportunities"` |
| `metadata` | `{stage, projected_revenue, contact_id}` |

### 4.3 Order → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{order['id']}"` |
| `source_id` | `order["id"]` |
| `title` | `order["title"] or f"Order {id}"` |
| `content` | `json.dumps(order["order_items"])` |
| `source` | `"keap.orders"` |
| `metadata` | `{total, status, contact_id}` |

## 5. Key API Endpoints & Methods

| KeapConnector method | HTTP | Path | Notes |
|---|---|---|---|
| `list_contacts(limit, offset, email?, given_name?, family_name?)` | GET | `/contacts` | offset/limit pagination |
| `get_contact(id)` | GET | `/contacts/{id}` | |
| `create_contact(given_name?, family_name?, email_addresses?, phone_numbers?)` | POST | `/contacts` | |
| `update_contact(id, fields)` | PATCH | `/contacts/{id}` | partial update |
| `delete_contact(id)` | DELETE | `/contacts/{id}` | |
| `list_opportunities(limit, offset)` | GET | `/opportunities` | |
| `create_opportunity(title, contact_id, stage_id, projected_revenue?)` | POST | `/opportunities` | |
| `list_orders(limit, offset)` | GET | `/orders` | |
| `list_tags()` | GET | `/tags` | |
| `apply_tag(tag_id, contact_ids[])` | POST | `/tags/{tag_id}/contacts` | |
| `remove_tag(tag_id, contact_id)` | DELETE | `/tags/{tag_id}/contacts/{contact_id}` | |
| `list_campaigns(limit)` | GET | `/campaigns` | |
| `add_contact_to_campaign(campaign_id, sequence_id, contact_id)` | POST | `/campaigns/{cid}/sequences/{sid}/contacts/{contact_id}` | |
| `send_email(contact_ids[], subject, html_content?, plain_content?)` | POST | `/emails/queue` | |
| `health_check()` | GET | `/account/profile` | |

## 6. Error Handling

`client.http_client.KeapHTTPClient._raise_for_status` maps every non-2xx into a Keap-specific exception:

| HTTP | Exception | `_STATUS_MAP` (health, auth) |
|---|---|---|
| 401 / 403 | `KeapAuthError` | `(DEGRADED, TOKEN_EXPIRED)` / `(UNHEALTHY, FAILED)` |
| 404 | `KeapNotFound` | `(HEALTHY, CONNECTED)` |
| 429 | `KeapRateLimitError(retry_after)` | `(DEGRADED, CONNECTED)` |
| 5xx | `KeapNetworkError` | `(OFFLINE, CONNECTED)` |
| other 4xx | `KeapError` | inherited |

`helpers.utils.with_retry` retries `KeapRateLimitError`, `KeapNetworkError`, and `httpx.TransportError` with exponential backoff + jitter; honours `Retry-After` on the first attempt.

The HTTP client's 401-recovery path invokes the connector-supplied `token_refresher` exactly once per request — repeated 401 after refresh surfaces immediately as `KeapAuthError`.

## 7. Testing Strategy

- `tests/conftest.py` — `sys.path.insert` for both the connector root and the monorepo `shared.base_connector` path. Autouse `mock_storage` patches every `BaseConnector` storage side-effect (`set_token`, `clear_token`, `save_config`, `ingest_batch`, `ingest_document`, `get_metadata`, `set_metadata`, `get_token`). Autouse `mock_logger` silences structlog. `mock_KeapHTTPClient` fixture patches `connector.KeapHTTPClient` BEFORE `__init__`. `no_retry_sleep` stubs `helpers.utils.asyncio.sleep` for fast retry tests.
- `tests/test_connector.py` — covers install (success / missing creds), OAuth authorize, health_check (healthy / 401), list_contacts (pagination + filter), create_contact, list_opportunities / list_tags / apply_tag, refresh-on-401 transparent recovery, retry-on-429, KeapAuthError surface, connector identity (CONNECTOR_TYPE / AUTH_TYPE / REQUIRED_CONFIG_KEYS), multi-tenant isolation, normalizer smoke.
- All tests use `httpx` + `respx` — zero real network, deterministic across CI.
- `pnpm`-equivalent: `PYTHONPATH="…/shielva-connectors/core" pytest tests/`.

## 8. Multi-Tenant Isolation

- `tenant_id` always sourced from the connector's auth context (constructor arg → `self.tenant_id`); never from env vars or hardcoded constants.
- `NormalizedDocument.id` is always `f"{tenant_id}_{source_id}"` so cross-tenant index collisions are impossible.
- Per-instance state — `_token_info`, `http_client` — is bound to `self`; two instances on different tenants share no mutable state.
- Test fixture `test_independent_instances_per_tenant` asserts this in CI.

## 9. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Refresh-token rotation: Keap rotates refresh tokens on every refresh. | `on_token_refresh` stores `data.get("refresh_token") or stored`; `set_token` persists immediately. |
| Long-lived deployments lose tokens on restart. | `BaseConnector.set_token` writes to Redis / DB via the platform-provided sink; not connector concern. |
| Keap publishes v2 endpoints with different envelopes. | `base_url` is install-configurable; v2-specific methods can land as additional `KeapHTTPClient` verbs without touching the connector body. |
| 429 storms from bulk sync. | `with_retry` + provider `Retry-After`; default `rate_limit_per_min=60`. |
| Naive vs tz-aware datetime mismatch (historical bug). | All token expiries use `datetime.now(timezone.utc)`; test fixtures mirror this. |
