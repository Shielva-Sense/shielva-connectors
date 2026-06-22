# AWS S3 Connector — Implementation Plan

> Step 0 artifact (`generate_implementation_plan`).
> Produced by following the Electron prompt at `builder.prompts.ts:235`.

## 1. Overview

**Amazon S3** is AWS's object-storage primitive. This connector — `AwsS3Connector`
(`CONNECTOR_TYPE = "aws_s3"`, `AUTH_TYPE = "api_key"`) — wraps the operational
surfaces a Shielva tenant typically needs from S3 (or any S3-compatible provider:
Cloudflare R2, Wasabi, Backblaze B2, MinIO, DigitalOcean Spaces):

| Surface | Operation | Capability |
|---|---|---|
| Buckets | `ListBuckets` / `CreateBucket` / `DeleteBucket` | Account-level bucket inventory + CRUD |
| Objects (read) | `ListObjectsV2` / `HeadObject` / `GetObject` | List, head, fetch object bodies |
| Objects (write) | `PutObject` / `CopyObject` / `DeleteObject` | Upload, server-side copy, delete |
| Versions | `ListObjectVersions` | Versioned-bucket inventory |
| ACLs | `PutObjectAcl` | Per-object access control |
| Presign | `generate_presigned_url` | Time-limited GET/PUT URLs — no server bytes transit |

The connector normalises S3 objects into `NormalizedDocument` (`id = f"{tenant_id}_{source_id}"`)
inside `helpers/normalizer.py`. All public methods are standalone `async def` on
`AwsS3Connector` (OCP — new ops add a method without modifying the base).

We use **AWS Signature V4** end-to-end via `aiobotocore` — the same signing
library AWS publishes for boto3, kept fully async so the connector never blocks
the event loop and never hand-rolls SigV4 (a known footgun).

## 2. SDK / Package Selection

| Package | Version | Justification |
|---|---|---|
| `aiobotocore` | `>=2.0` | Async S3 client. Owns AWS SigV4, retry orchestration, host-header construction, and S3-compatible endpoint quirks (path-style vs. virtual-hosted, R2 / Wasabi / Backblaze / MinIO). The session is created per-instance and lifecycled via `async with`. |
| `botocore` | `>=1.34` | Transitive dep of `aiobotocore`. Provides typed exception classes (`ClientError`, `EndpointConnectionError`) we map onto `AwsS3*` typed exceptions. |
| `structlog` | `>=24.1` | Mandatory per CONNECTOR_SYSTEM_PROMPT; pre-installed in the gateway venv. |
| `pytest`, `pytest-asyncio`, `pytest-mock` | pre-installed | Async test framework. |

We deliberately do NOT use raw `httpx` + a hand-rolled SigV4 signer — SigV4
canonicalisation is full of foot-guns (path-style payload hashing, header
case normalisation, multi-value query string encoding, chunked-uploads). AWS
maintains `botocore` for exactly this reason.

We deliberately do NOT use sync `boto3` wrapped in `asyncio.to_thread`. While
that pattern works, it serialises every call through the threadpool — a
deal-breaker for connectors that want to issue many concurrent `head_object`
calls during a sync. `aiobotocore` shares the same SigV4 + retry plumbing as
boto3 but lets the event loop multiplex.

## 3. Auth Flow

AWS S3 uses **Signature V4** request signing — every request is signed with the
caller's access key + secret key. There is no token exchange and no refresh
cycle for long-lived IAM keys (STS session tokens DO expire — see below).

### Credentials (per-tenant install_fields)

- `access_key_id` — IAM access key id (e.g. `AKIA…`). Required. Stored in `self.config["access_key_id"]`.
- `secret_access_key` — paired secret (40-char). Required. `type: secret`.
- `region` — AWS region code (default `us-east-1`). Optional.
- `endpoint_url` — S3-compatible endpoint override (Cloudflare R2, Wasabi, …). Optional.
- `session_token` — STS / federated identity session token. Optional, `type: secret`.
- `default_bucket` — UI convenience; not validated at install. Optional.

### Lifecycle

| Phase | Behaviour |
|---|---|
| `install()` | Validates `access_key_id` + `secret_access_key` are non-empty. Saves merged config. Does **not** call AWS. |
| `health_check()` | Calls `ListBuckets` — the smallest, IAM-scopeable read. 2xx → `HEALTHY+CONNECTED`. `AccessDenied`/`InvalidAccessKeyId` → `DEGRADED+EXPIRED`. `EndpointConnectionError` → `DEGRADED+CONNECTED` with message. |
| `sync()` | No-op for an object-storage primitive — S3 is not a knowledge-base source. Returns `SyncResult(COMPLETED, 0/0/0)` with explanatory message. Object ingestion is per-call: `list_objects` + `get_object` + caller-owned ingest. |
| `authorize()` | Not implemented — no OAuth flow. |

### Header / wire contract

`aiobotocore` constructs the canonical SigV4 request for every call. The connector layer never touches headers directly.

### Multi-tenant isolation

Each `AwsS3Connector` instance owns its own `S3Client` (lazy). Two tenants never share a session, a credential bundle, or an HTTP connection pool.

## 4. Data Model

S3 objects normalise to `NormalizedDocument` via `helpers/normalizer.normalize_object`:

| `NormalizedDocument` field | S3 source | Notes |
|---|---|---|
| `id` | `f"{tenant_id}_{bucket}/{key}"` | Tenant-scoped, deterministic |
| `source_id` | `f"{bucket}/{key}"` | Bucket-qualified key |
| `title` | `key.rsplit("/", 1)[-1]` | The filename component |
| `content` | `""` (deferred) | Bodies are NOT eagerly fetched in `list_objects` — call `get_object` to retrieve bytes |
| `content_type` | `obj["ContentType"]` when present | Falls back to `"application/octet-stream"` |
| `source_url` | `s3://{bucket}/{key}` | Canonical S3 URI |
| `created_at` | `obj["LastModified"]` | S3 has no created-at on the object — `LastModified` is the closest analogue |
| `updated_at` | `obj["LastModified"]` | |
| `metadata` | `{bucket, key, size, etag, storage_class, version_id}` | All key wire fields |

`helpers/normalizer.py` ALSO exposes `normalize_bucket(raw, tenant_id)` for
account-level inventory (used by `sync()` if a tenant ever opts in).

## 5. Key API Endpoints & Methods

All public async methods on `AwsS3Connector`. SOC: `connector.py` orchestrates
only — every AWS call is delegated to `client/http_client.py::S3HTTPClient`.

### 5.1 Lifecycle

- `async install() -> ConnectorStatus`
  Validate `access_key_id` + `secret_access_key`. Save merged config. Never calls AWS.
- `async health_check() -> ConnectorStatus`
  `ListBuckets` probe. Classifies failures via `_STATUS_MAP`.
- `async sync(since=None, full=False, kb_id=None, webhook_url=None) -> SyncResult`
  No-op (`SyncResult(COMPLETED, 0/0/0)`).

### 5.2 Buckets

- `async list_buckets() -> List[Dict]`
  → `[{name, creation_date(ISO-8601)}]`.
- `async create_bucket(bucket, region=None) -> Dict`
  Honours the `us-east-1` quirk (CreateBucket rejects `LocationConstraint` for us-east-1).
- `async delete_bucket(bucket) -> Dict`
  Bucket MUST be empty (AWS enforces server-side).
- `async head_bucket(bucket) -> Dict`
  Existence + access probe — used as an optional health override when `default_bucket` is set.

### 5.3 Objects

- `async list_objects(bucket, prefix="", max_keys=1000, continuation_token=None) -> Dict`
  `ListObjectsV2` with token pagination. Returns `{bucket, prefix, objects, is_truncated, next_continuation_token, key_count}`.
- `async list_object_versions(bucket, prefix="", max_keys=1000) -> Dict`
  Versioned-bucket inventory.
- `async get_object_metadata(bucket, key) -> Dict`
  `HeadObject` → `{bucket, key, size, etag, content_type, last_modified, metadata, version_id}`.
- `async get_object(bucket, key) -> bytes`
  `GetObject` body drained into memory. For very large objects, prefer `generate_presigned_url("get_object")`.
- `async put_object(bucket, key, body, content_type, metadata=None) -> Dict`
  `PutObject` with user metadata sanitisation (ASCII strings only).
- `async copy_object(source_bucket, source_key, dest_bucket, dest_key) -> Dict`
  Server-side copy. No body transit.
- `async delete_object(bucket, key) -> Dict`
  Idempotent on AWS — succeeds even when the key is missing.
- `async set_object_acl(bucket, key, acl) -> Dict`
  `PutObjectAcl`. `acl ∈ {"private", "public-read", "public-read-write", "authenticated-read", …}`.

### 5.4 Presigned URLs

- `async generate_presigned_url(bucket, key, operation="get_object", expires_in=3600) -> Dict`
  Local SigV4 signing — no network round-trip. AWS caps SigV4 GET URLs at 7 days.

## 6. Error Handling

Exception hierarchy in `exceptions.py`:

```
AwsS3Error                # base; carries status_code + response_body
├── AwsS3AuthError        # 401 / 403 — AccessDenied, InvalidAccessKeyId, SignatureDoesNotMatch, ExpiredToken
├── AwsS3NotFound         # 404 — NoSuchKey, NoSuchBucket
└── AwsS3NetworkError     # transport — DNS, socket, TLS, EndpointConnectionError, 5xx
```

### Classification

`helpers/utils.classify_client_error` maps `botocore.exceptions.ClientError` and
`EndpointConnectionError` to typed exceptions based on `Error.Code` and HTTP
status — independent of which `aiobotocore` operation raised them.

### Retry behaviour (`helpers/utils.with_retry`)

| Class | Action |
|---|---|
| `AwsS3AuthError` / `AwsS3NotFound` | Raise immediately — no retry |
| `AwsS3NetworkError` | Exponential backoff with jitter, max 3 attempts |
| `AwsS3Error` with `status_code` 0 or 5xx | Same backoff |
| `AwsS3Error` with `status_code` in {400, 409, …} | Raise immediately |

Backoff sequence: `RETRY_DELAY_S * BACKOFF_FACTOR ** attempt + uniform(0, 0.5)`,
capped at `MAX_RETRY_DELAY_S`.

### How errors surface to the gateway

`health_check()` catches typed exceptions and maps via `_STATUS_MAP`:

```
401 / AccessDenied        → ConnectorStatus(DEGRADED, EXPIRED)
403 / InvalidAccessKeyId  → ConnectorStatus(DEGRADED, EXPIRED)
NoSuchBucket on default   → ConnectorStatus(DEGRADED, CONNECTED)
EndpointConnectionError   → ConnectorStatus(DEGRADED, CONNECTED)
other                     → ConnectorStatus(DEGRADED, CONNECTED) with message
```

## 7. Dependencies

```
aiobotocore>=2.0
botocore>=1.34
```

(`structlog`, `pytest`, `pytest-asyncio`, `pytest-mock` are pre-installed by the gateway venv.)

## 8. Config & Install Fields

| Key | Type | Required | Source | Purpose |
|---|---|---|---|---|
| `access_key_id` | string | yes | install_field | IAM access key id (`AKIA…`) |
| `secret_access_key` | secret | yes | install_field | Paired secret (40 chars) |
| `region` | string | no (default `us-east-1`) | install_field | AWS region code |
| `endpoint_url` | string | no | install_field | Override for R2 / Wasabi / Backblaze / MinIO |
| `session_token` | secret | no | install_field | STS / federated-identity token |
| `default_bucket` | string | no | install_field | UI convenience pre-fill |

In `connector.py`:

```python
REQUIRED_CONFIG_KEYS = ["access_key_id", "secret_access_key", "region"]
_STATUS_MAP = {
    401: ("DEGRADED", "EXPIRED"),
    403: ("DEGRADED", "EXPIRED"),
    429: ("DEGRADED", "CONNECTED"),
}
```

`region` is in `REQUIRED_CONFIG_KEYS` (per the public catalogue contract) but
the constructor defaults missing values to `"us-east-1"` so a tenant that omits
it is still served — the required-key check is a wire-shape requirement, not a
behavioural gate.

## 9. SOC / OCP Architecture Plan

| File | Responsibility | Imports |
|---|---|---|
| `connector.py` | Orchestrator only. Public method surface. Lifecycle methods. **No raw AWS calls, no JSON parsing.** | `shared.base_connector`, `client.http_client`, `helpers.normalizer`, `helpers.utils`, `exceptions`, `structlog` |
| `client/http_client.py` | Single owner of the `aiobotocore` session. Owns SigV4, retry orchestration, exception translation. | `aiobotocore.session`, `botocore.exceptions`, `helpers.utils`, `exceptions`, `structlog` |
| `helpers/normalizer.py` | Maps raw S3 payloads → `NormalizedDocument`. | `shared.base_connector.NormalizedDocument` |
| `helpers/utils.py` | `classify_client_error`, `with_retry`, `sanitize_metadata`, ISO date helpers. | `botocore.exceptions`, `exceptions`, `structlog` |
| `models.py` | Local request/response dataclasses + enum shims for `AuthStatus` / `ConnectorHealth`. | `shared.base_connector` |
| `exceptions.py` | `AwsS3Error` hierarchy. | (stdlib) |
| `__init__.py` | Re-export `AwsS3Connector`. | `connector` |

SOC/OCP self-check:

1. `connector.py` orchestrates only ✓
2. AWS calls live in `client/http_client.py` ✓
3. Response transforms in `helpers/normalizer.py` ✓
4. Utilities in `helpers/utils.py` ✓
5. `connector.py` imports from `client/` + `helpers/` ✓
6. Every user-named method is a standalone `async def` ✓
7. New ops added without modifying `BaseConnector` ✓
8. Config via `self.config.get(...)` — never hardcoded ✓
9. Features (retry, pagination, presigning) as composable helpers ✓
10. Error mapping in `exceptions.py`; `connector.py` only catches typed exceptions ✓

**Score: 10/10.**
