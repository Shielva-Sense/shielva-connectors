# Firebase Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Firebase** is Google's mobile/web app platform: real-time data (Firestore + Realtime Database), authentication (Identity Toolkit), push notifications (FCM v1), and object storage (Cloud Storage for Firebase). This connector — `FirebaseConnector` (`CONNECTOR_TYPE = "firebase"`, `AUTH_TYPE = "service_account"`) — wraps the five operational surfaces a Shielva tenant typically needs from a Firebase project:

| Surface | Base path | Capability |
|---|---|---|
| Firestore | `https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)/documents` | CRUD on documents in a collection |
| Realtime DB | `https://{database_name}.firebaseio.com` | Read / write a JSON tree by path |
| Identity Toolkit (Auth) | `https://identitytoolkit.googleapis.com/v1/projects/{project_id}/accounts` | List, get, create, update, delete users |
| FCM v1 | `https://fcm.googleapis.com/v1/projects/{project_id}/messages:send` | Send push notifications by token or topic |
| Cloud Storage | `https://firebasestorage.googleapis.com/v0/b/{bucket}/o/{name}` | List + upload objects in a Firebase Storage bucket |

The connector normalises Firestore documents and Identity Toolkit users into `NormalizedDocument` (id = `f"{tenant_id}_{source_id}"`), exposes one standalone public `async def` per user-requested operation (OCP), and never embeds raw HTTP in `connector.py` — every transport call is delegated to `client/http_client.py::FirebaseHTTPClient`.

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `httpx` | `>=0.27,<1.0` | Async client; pre-installed in shared venv |
| `pydantic` | `>=2.0` | Request body schemas; pre-installed |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed |
| `PyJWT[crypto]` | `>=2.8,<3.0` | RS256 sign of the service-account JWT assertion (needs `cryptography` extra for RSA private-key parsing) |
| `cryptography` | `>=42.0` | Pulled in transitively by PyJWT crypto extra; also used by tests to mint throwaway RSA keys |

Pre-installed (do NOT re-add): `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`.

## 3. Auth Flow

Firebase exposes its admin REST surface behind Google's standard **service-account JWT-bearer OAuth2** flow.

### Credentials
- `service_account_json` — Full service-account JSON downloaded from **Firebase Console → Project Settings → Service Accounts → Generate New Private Key**. Stored as an install_field (type `secret`, input_type `textarea`, required). The connector derives `project_id`, `client_email`, `private_key`, `token_uri` from inside this blob — so no separate `project_id` field is required.
- `database_url` — Optional Realtime Database URL (`https://{db}.firebaseio.com`). install_field (type `string`, optional). Only required when `get_realtime_db` / `set_realtime_db` are called.
- `storage_bucket` — Optional default Cloud Storage bucket name (e.g. `my-project.appspot.com`). install_field (type `string`, optional). Falls back to `{project_id}.appspot.com` when blank.
- `scopes` — Optional space-separated OAuth2 scope override. install_field (type `string`, optional). Defaults cover Firestore, RTDB, FCM, Identity Toolkit, and Storage.

### Header contract
Every authenticated request to `*.googleapis.com` / `firebasestorage.googleapis.com`:

```
Authorization: Bearer <access_token>
Content-Type:   application/json
```

`access_token` is minted by signing an RS256 JWT (`iss=client_email, scope=<scopes>, aud=token_uri, iat=now, exp=now+3600`) with the service-account private key and POSTing it to `https://oauth2.googleapis.com/token` with `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`. The token is cached in-process behind an `asyncio.Lock` and re-minted 60 s before expiry.

### Lifecycle
- `install()` parses the service-account JSON, mints an initial access token (proving the credential is live), persists the non-secret pieces (`project_id`, `client_email`, `database_url`, `storage_bucket`), and returns `ConnectorStatus(HEALTHY, AUTHENTICATED)`.
- `authorize()` returns a `TokenInfo` wrapping the cached access token — there is no interactive OAuth.
- `health_check()` mints/refreshes the token and probes a sentinel Firestore collection (`__shielva_health__`). A 404 is treated as success — only auth / network failures degrade health.
- `ensure_token()` — handled internally by `FirebaseHTTPClient.get_access_token()` before every request.

## 4. Data Model

### 4.1 Firestore document → NormalizedDocument

| NormalizedDocument | Firestore JSON | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{document_id}"` | tenant-scoped |
| `source_id` | last segment of `document["name"]` | Firestore-generated doc id |
| `title` | `fields["title"] / fields["name"] / document_id` | falls back gracefully |
| `content` | str(decoded_fields) | full field map flattened |
| `source` | `"firebase.firestore"` | |
| `created_at` | `document["createTime"]` | RFC 3339 |
| `updated_at` | `document["updateTime"]` | |
| `metadata` | `{firestore_name, fields, collection}` | |

### 4.2 Identity Toolkit user → NormalizedDocument

| field | from |
|---|---|
| `id` | `f"{tenant_id}_{user['localId']}"` |
| `source_id` | `user["localId"]` |
| `title` | `user["email"] / user["displayName"] / localId` |
| `content` | `f"email={email} verified={emailVerified} disabled={disabled}"` |
| `source` | `"firebase.auth"` |
| `created_at` | epoch-ms `user["createdAt"]` → datetime |
| `updated_at` | epoch-ms `user["lastLoginAt"]` → datetime |
| `metadata` | `{email, emailVerified, disabled, providerUserInfo}` |

## 5. Key API Endpoints & Methods

Every method listed in `plan_steps.json::write_connector.config.methods` MUST exist as a standalone public `async def` in `connector.py`.

| Method | HTTP | Path | Notes |
|---|---|---|---|
| `install()` | (lifecycle) | n/a | Parse SA JSON → mint access token → persist `client_email`. |
| `health_check()` | GET | Firestore `__shielva_health__` list | 404 is OK; only auth/network failures degrade. |
| `sync(since, full, kb_id, webhook_url)` | (lifecycle) | iterates configured Firestore collections | Calls `ingest_document`. Empty by default. |
| `list_documents(collection, *, page_size, page_token)` | GET | Firestore `/{collection}` | Cursor pagination via `pageToken`. |
| `get_document(collection, document_id)` | GET | Firestore `/{collection}/{document_id}` | |
| `create_document(collection, fields, document_id=None)` | POST | Firestore `/{collection}` | Auto-encodes fields → Firestore Value. |
| `update_document(collection, document_id, fields)` | PATCH | Firestore `/{collection}/{document_id}` | |
| `delete_document(collection, document_id)` | DELETE | Firestore `/{collection}/{document_id}` | |
| `list_users(*, page_size=1000, next_page_token=None)` | POST | Identity Toolkit `/accounts:batchGet` | Cursor pagination via `nextPageToken`. |
| `get_user(uid)` | POST | Identity Toolkit `/accounts:lookup` | Body: `{"localId": [uid]}`. |
| `create_user(email, password=None, display_name=None, phone_number=None)` | POST | `https://identitytoolkit.googleapis.com/v1/accounts` | Admin user-create endpoint. |
| `update_user(uid, *, email=None, password=None, display_name=None, disabled=None)` | POST | Identity Toolkit `/accounts:update` | Partial-update by `localId`. |
| `delete_user(uid)` | POST | Identity Toolkit `/accounts:delete` | Body: `{"localId": uid}`. |
| `send_fcm_notification(token=None, topic=None, notification=None, data=None, android=None, apns=None)` | POST | FCM `/messages:send` | Either `token` or `topic` required. |
| `get_realtime_db(path)` | GET | `https://{db}.firebaseio.com/{path}.json` | Requires `database_url`. |
| `set_realtime_db(path, data)` | PUT | `https://{db}.firebaseio.com/{path}.json` | Full overwrite at path. |
| `list_storage_objects(*, bucket=None, prefix=None, page_size=100, page_token=None)` | GET | `https://firebasestorage.googleapis.com/v0/b/{bucket}/o` | Default bucket from install field. |
| `upload_storage_object(name, data, *, content_type="application/octet-stream", bucket=None)` | POST | `https://firebasestorage.googleapis.com/v0/b/{bucket}/o?name={name}` | Body is the raw object bytes. |

Wire convention: Firestore wraps values as `{"stringValue": "..."}` / `{"integerValue": "5"}` envelopes — the connector encodes Python → Firestore at the boundary in `client/http_client.py` and decodes Firestore → Python in `helpers/normalizer.py`.

## 6. Error Handling

| HTTP | Firebase meaning | Mapped to |
|---|---|---|
| 400 | Bad request | `FirebaseBadRequestError` (raise) |
| 401 | Access token invalid / missing | `FirebaseAuthError` → `AuthStatus.TOKEN_EXPIRED` + `ConnectorHealth.OFFLINE` |
| 403 | Forbidden (service account lacks IAM role) | `FirebaseAuthError` → `AuthStatus.INVALID_CREDENTIALS` + `ConnectorHealth.UNHEALTHY` |
| 404 | Not found | `FirebaseNotFoundError` (raise) |
| 409 | Conflict (duplicate email / doc revision mismatch) | `FirebaseConflictError` |
| 429 | Quota exhausted (Firebase returns no `Retry-After`) | `FirebaseRateLimitError` → `ConnectorHealth.DEGRADED` |
| 5xx | Provider outage | `FirebaseServerError` → retry with exponential backoff |

All in `exceptions.py` extending `FirebaseError`. Retry in `client/http_client.py::_request` honours `max_retries=3`, exponential backoff `min(0.5 * 2 ** attempt, 8)` for 5xx, fixed 5 s for 429.

## 7. Dependencies

Packages to install in connector's venv (`install_deps` reads this section):

```
PyJWT[crypto]>=2.8
cryptography>=42.0
```

(httpx, pydantic, structlog, pytest, pytest-asyncio, pytest-mock, respx are pre-installed.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Notes |
|---|---|---|---|---|
| `service_account_json` | secret | yes | install_field (textarea) | Full SA JSON; `project_id`/`client_email`/`private_key` derived from it |
| `database_url` | string | no | install_field | Realtime DB URL; required only when `get_realtime_db` / `set_realtime_db` are used |
| `storage_bucket` | string | no | install_field | Default Cloud Storage bucket; falls back to `{project_id}.appspot.com` |
| `scopes` | string | no | install_field | Space-separated OAuth2 scope override |
| `timeout_s` | number | no | install_field (default 30) | Per-request httpx timeout |

In `connector.py`:
```python
REQUIRED_CONFIG_KEYS = ["service_account_json"]
_STATUS_MAP = {
    401: ("OFFLINE",   "TOKEN_EXPIRED"),
    403: ("UNHEALTHY", "INVALID_CREDENTIALS"),
    429: ("DEGRADED",  "CONNECTED"),
}
```

## 9. SOC/OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public API surface. Lifecycle methods. **No raw HTTP, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of httpx. Builds JWT, mints + caches access tokens, retries, encodes Firestore Values, raises typed exceptions. | `httpx`, `jwt`, `structlog`, `exceptions` |
| `helpers/normalizer.py` | Maps raw Firestore docs + Identity Toolkit users → `NormalizedDocument`. Decodes Firestore Value envelopes. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `parse_service_account_json`, generic retry, epoch-ms → datetime. | (stdlib only) |
| `models.py` | Pydantic schemas for FCM message + Auth update bodies. | `pydantic` |
| `exceptions.py` | `FirebaseError` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `FirebaseConnector`. | `connector` |

SOC/OCP self-check:
1. `connector.py` orchestrates only ✓
2. HTTP in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is standalone `async def` ✓
7. New ops added without modifying BaseConnector ✓
8. Config via `self.config.get(...)` ✓
9. Features (retry, token cache, Firestore-Value encoding) as composable helpers ✓
10. Error mapping in `exceptions.py`; connector.py catches custom exceptions only ✓

**Score: 10/10.**
