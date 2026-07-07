"""Cloudflare R2 integration for caching integration prompts and plans.

Tenant-root-bucket architecture:
  Bucket : {R2_BUCKET_NAME}              e.g. "shielvasense"
  Prefix : {R2_COLLECTION_PREFIX}/       e.g. "integration-plans/"

Full key layout per provider/service (no tenant subfolder — bucket is tenant-scoped):
  {collection}/{provider}/{service}/prompts.csv          — prompt history (append-only)
  {collection}/{provider}/{service}/plan.json            — latest generated plan
  {collection}/{provider}/{service}/plan.md              — human-readable plan
  {collection}/{provider}/{service}/progress.json        — authoritative cache status
  {collection}/{provider}/{service}/failures/            — failed step outputs
  {collection}/STEP_PROMPTS/{prompt_name}.txt            — versioned LLM prompts

When R2 is NOT configured, falls back to local filesystem at ./plan_cache/.
"""

import asyncio
import contextlib
import csv
import io
import json
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import structlog

from integration.core.config import settings

# ── Per-request bucket resolution ────────────────────────────────────────────
# Priority (highest → lowest):
#   1. R2_BUCKET_NAME env var  — explicit per-deployment override, always wins
#   2. _app_bucket_ctx         — set from X-App-ID header: "shielva-agentic-app-{app_id}"
#   3. _tenant_bucket_ctx      — set from X-Tenant-Name header (legacy / post-login)
_tenant_bucket_ctx: ContextVar[str] = ContextVar("tenant_bucket", default="")
_app_bucket_ctx: ContextVar[str] = ContextVar("app_bucket", default="")


def app_id_to_bucket(app_id: str) -> str:
    """Derive the R2 bucket name from a stable app installation ID.
    Convention: shielva-agentic-app-{app_id}
    """
    return f"shielva-agentic-app-{app_id}"


def _get_shared_bucket() -> str:
    """Return the fixed shared R2 bucket that holds global/shared read-only resources.

    This bucket (default: "shielvasense") contains:
      - STEP_PROMPTS/          — versioned LLM prompts managed by Shielva admins
      - CODE_EXECUTION_GUIDELINES/  — connector development standards
      - CONNECTOR_DOCUMENTATION_GUIDELINES/
      - METADATA_WRITING_GUIDELINES/
      - INSTRUCTION_SETUP_GUIDELINES/

    It is the same bucket for every app installation and never contains
    per-connector or per-session data.
    """
    return settings.R2_SHARED_BUCKET or "shielvasense"


def _get_bucket() -> str:
    """Return the per-app R2 bucket for the current request context.

    Used for all connector-specific data: plan.json, progress.json, execution
    state, generated code, test guidelines, setup instructions, etc.

    Priority:
      1. settings.R2_BUCKET_NAME — explicit .env override (always wins)
      2. _app_bucket_ctx  — "shielva-agentic-app-{app_id}" set from X-App-ID header
                            (covers both pre-login and post-login for the same installation)
      3. _tenant_bucket_ctx — tenant name from X-Tenant-Name header (legacy fallback)
    """
    return settings.R2_BUCKET_NAME or _app_bucket_ctx.get() or _tenant_bucket_ctx.get()


logger = structlog.get_logger(__name__)

# ── Local cache directory (used when R2 is not configured) ───────────
# Lives INSIDE GENERATED_CODE_DIR (not its parent) so that all user data stays
# in the user's chosen project directory, never inside the shielva-connectors repo.
# Layout: {GENERATED_CODE_DIR}/plan_cache/{collection_prefix}/{provider}/{slug}/
_LOCAL_CACHE_DIR = Path(settings.GENERATED_CODE_DIR).resolve() / "plan_cache"

# ── Step type display labels ──────────────────────────────────────────

_STEP_TYPE_ICONS = {
    "install_deps": "📦",
    "configure_auth": "🔑",
    "scaffold_code": "🏗",
    "write_connector": "⚙",
    "write_tests": "🧪",
    "run_tests": "▶",
}

_STEP_TYPE_LABELS = {
    "install_deps": "Install Dependencies",
    "configure_auth": "Configure Authentication",
    "scaffold_code": "Scaffold Code Structure",
    "write_connector": "Write Connector",
    "write_tests": "Write Tests",
    "run_tests": "Run Tests",
}


# ── Markdown generator ────────────────────────────────────────────────


def _build_plan_markdown(
    provider: str,
    service: str,
    prompt: str,
    plan_data: dict[str, Any],
    generated_at: str,
) -> str:
    """Convert a plan_data dict into a full human-readable markdown document."""
    steps: list[dict[str, Any]] = plan_data.get("steps", [])
    version: int = plan_data.get("version", 1)
    package_structure: dict[str, Any] | None = plan_data.get("package_structure")
    recommended_features: list[dict[str, Any]] = plan_data.get("recommended_features", [])

    lines: list[str] = []

    # ── Title ──
    lines += [
        f"# Integration Plan — {provider.title()} / {service.replace('-', ' ').title()}",
        "",
        f"> **Version:** v{version}  ",
        f"> **Generated:** {generated_at}  ",
        f"> **Provider:** `{provider}`  ",
        f"> **Service:** `{service}`  ",
        "",
    ]

    # ── Prompt ──
    if prompt:
        lines += [
            "## Prompt",
            "",
            f"> {prompt}",
            "",
        ]

    # ── Steps ──
    lines += [
        f"## Plan Steps ({len(steps)} steps)",
        "",
    ]
    for step in steps:
        idx = step.get("index", 0)
        stype = step.get("type", "")
        icon = _STEP_TYPE_ICONS.get(stype, "▸")
        label = _STEP_TYPE_LABELS.get(stype, stype)
        title = step.get("title", f"Step {idx + 1}")
        description = step.get("description", "")
        est = step.get("estimated_duration_s", 30)
        config = step.get("config", {})

        lines += [
            f"### {icon} Step {idx + 1}: {title}",
            "",
            f"**Type:** `{label}`  ",
            f"**Estimated duration:** {est}s  ",
            "",
        ]

        if description:
            lines += [description, ""]

        # Packages (install_deps steps often have a packages list in config)
        packages = config.get("packages") or config.get("dependencies") or []
        if packages:
            if isinstance(packages, list):
                lines += ["**Packages:**", ""]
                lines += [f"```\n{chr(10).join(packages)}\n```", ""]
            elif isinstance(packages, str):
                lines += ["**Packages:**", "", f"```\n{packages}\n```", ""]

        # Files (scaffold/write steps often list files in config)
        files = config.get("files") or config.get("file_list") or []
        if files:
            lines += ["**Files:**", ""]
            if isinstance(files, list):
                for f in files:
                    if isinstance(f, dict):
                        lines.append(f"- `{f.get('path', f)}` — {f.get('description', '')}")
                    else:
                        lines.append(f"- `{f}`")
                lines.append("")

        # Any other config keys
        for key, val in config.items():
            if key in ("packages", "dependencies", "files", "file_list"):
                continue
            if val:
                lines += [f"**{key.replace('_', ' ').title()}:** {val}  "]
        lines.append("")

    # ── Package Structure ──
    if package_structure:
        root = package_structure.get("root", "")
        pkg_files: list[dict[str, Any]] = package_structure.get("files", [])
        lines += [
            f"## Package Structure ({len(pkg_files)} files)",
            "",
            f"Root: `{root}`",
            "",
            "```",
        ]
        for pf in pkg_files:
            path = pf.get("path", "")
            desc = pf.get("description", "")
            lines.append(f"{path}{'  — ' + desc if desc else ''}")
        lines += ["```", ""]

    # ── Recommended Features ──
    if recommended_features:
        lines += [
            f"## Recommended Features ({len(recommended_features)} features)",
            "",
        ]
        # Group by category
        by_cat: dict[str, list[dict]] = {}
        for feat in recommended_features:
            cat = feat.get("category", "other")
            by_cat.setdefault(cat, []).append(feat)

        for cat, feats in by_cat.items():
            lines += [f"### {cat.title()}", ""]
            for feat in feats:
                rec = "✅" if feat.get("recommended") else "○"
                lines.append(f"- {rec} **{feat.get('label', feat.get('id', ''))}** — {feat.get('description', '')}")
            lines.append("")

    lines += [
        "---",
        f"*Generated by Shielva Integration Builder on {generated_at}*",
    ]

    return "\n".join(lines)


# ── Storage mode detection ───────────────────────────────────────────


def is_configured() -> bool:
    """Return True if R2 credentials are present.

    Bucket is resolved per-request from X-App-ID (or X-Tenant-Name for legacy).
    We intentionally do NOT require _get_bucket() here — the bucket is always
    resolvable at request time even when R2_BUCKET_NAME is left empty in .env.
    """
    return bool(settings.R2_ACCOUNT_ID and settings.R2_ACCESS_KEY_ID and settings.R2_SECRET_ACCESS_KEY)


def _use_local() -> bool:
    """True when R2 credentials are not configured — use local filesystem fallback.

    Falls back to local FS also when the per-request bucket cannot be resolved
    (e.g. startup-time calls with no request context and no R2_BUCKET_NAME set).
    """
    if not is_configured():
        return True
    # At request time: use local FS only if no bucket can be resolved
    bucket = _get_bucket()
    return not bucket


# ── Local filesystem helpers ─────────────────────────────────────────


def _local_path(key: str) -> Path:
    """Convert an R2-style key to a local filesystem path."""
    return _LOCAL_CACHE_DIR / key


# ── Local-disk fallback DISABLED ─────────────────────────────────────────────
# The connector CLI generates everything locally in the connector's .shielva/ dir,
# and the backend's durable store is MongoDB (plan lives in the session doc, docs in
# session.docs_json) with R2 as the optional cloud layer. Writing a second copy to
# local disk only polluted the repo (it created generated_connectors/plan_cache/...
# via mkdir). So when R2 is not configured we persist nothing to disk — reads return
# None and callers fall back to Mongo.
def _local_read(key: str) -> str | None:
    return None


def _local_write(key: str, content: str) -> None:
    return None


def _local_delete(key: str) -> bool:
    return False


# ── R2 helpers ────────────────────────────────────────────────────────


def _get_client():
    """Create a boto3 S3 client pointed at Cloudflare R2."""
    import boto3  # lazy import — only needed when R2 is configured

    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def _coll() -> str:
    """Return the collection key prefix for the current bucket context (no trailing slash).

    Per-app buckets (shielva-agentic-app-*) are already namespaced by the bucket
    name itself, so no prefix is needed — returns "".

    Legacy/shared buckets (shielvasense, tenant-name) namespace connector data
    inside the shared bucket using the configured prefix.

    Examples:
      shielva-agentic-app-abc123  →  ""
        key: google/shielva_gmail_5403cd/plan.json

      shielvasense                →  "shielvasense-integration-plans"
        key: shielvasense-integration-plans/google/shielva_gmail_5403cd/plan.json
    """
    bucket = _get_bucket()
    if bucket.startswith("shielva-agentic-app-"):
        return ""
    return settings.R2_COLLECTION_PREFIX


def _k(*parts: str) -> str:
    """Build an R2 key from path segments, dropping empty parts (safe prefix handling).

    Prevents leading slashes when the collection prefix is "" for per-app buckets:
      _k("", "google", "slug", "plan.json")  →  "google/slug/plan.json"
      _k("shielvasense-integration-plans", "google", "slug", "plan.json")
        →  "shielvasense-integration-plans/google/slug/plan.json"
    """
    return "/".join(p for p in parts if p)


def _csv_key(provider: str, service_slug: str, tenant_id: str = "") -> str:
    # tenant_id removed from path — the R2 bucket itself is already tenant-scoped
    return _k(_coll(), provider, service_slug, "prompts.csv")


def _plan_json_key(provider: str, service_slug: str, tenant_id: str = "") -> str:
    return _k(_coll(), provider, service_slug, "plan.json")


def _plan_md_key(provider: str, service_slug: str, tenant_id: str = "") -> str:
    return _k(_coll(), provider, service_slug, "plan.md")


def _progress_key(provider: str, service_slug: str, tenant_id: str = "") -> str:
    return _k(_coll(), provider, service_slug, "progress.json")


def _sync_read(client, bucket: str, key: str) -> str | None:
    """Read an object from R2 and return its body as a UTF-8 string.

    Transparent decompression: if the stored object was uploaded with
    ``Content-Encoding: gzip`` (every blob written through ``_sync_write``
    larger than ``_GZIP_MIN_BYTES``), we gunzip the body before returning.
    Older objects without the header come back as plain UTF-8 — no behavior
    change for them.
    """
    import gzip as _gzip

    from botocore.exceptions import ClientError

    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        raw = resp["Body"].read()
        if resp.get("ContentEncoding") == "gzip":
            try:
                raw = _gzip.decompress(raw)
            except Exception:  # corrupted gzip stream — surface raw instead of crashing
                pass
        return raw.decode("utf-8")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NoSuchBucket"):
            return None
        raise


# Bodies smaller than this are stored uncompressed — the gzip framing overhead
# costs more than the savings on tiny payloads, and many R2 viewers can't
# preview gzipped objects. JSON/markdown blobs of any meaningful size
# (plan_full.json, connector_docs.json, step_outputs/*.log, conversation
# history) all comfortably clear this threshold.
_GZIP_MIN_BYTES = 1024


def _sync_write(client, bucket: str, key: str, content: str, content_type: str = "text/plain") -> None:
    """Write a string to R2, gzip-compressed when the body is large enough.

    Compression ratio for our payloads (plan JSON, docs JSON, step logs,
    conversation history) is typically 4–10× because they're highly
    redundant Claude-generated text. The encoded body carries
    ``Content-Encoding: gzip`` so readers (and ``_sync_read``) know to
    decompress; the ``Content-Type`` header keeps the logical MIME so
    cache invalidation / proxies behave correctly.
    """
    import gzip as _gzip

    body = content.encode("utf-8")
    extra: dict = {}
    if len(body) >= _GZIP_MIN_BYTES:
        body = _gzip.compress(body, compresslevel=6)
        extra["ContentEncoding"] = "gzip"
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        **extra,
    )


def ensure_bucket() -> None:
    """Ensure the per-app R2 bucket exists, creating it if necessary.

    Per-app buckets (shielva-agentic-app-{app_id}) are created on-demand the
    first time a plan is saved for an installation.  The shared bucket
    (shielvasense) is pre-existing and managed separately.

    Called from save_prompt_and_plan() at request time — _get_bucket() will
    return the correct per-app bucket from the X-App-ID ContextVar.
    """
    if _use_local():
        # Local-disk fallback is disabled — nothing is persisted to disk (Mongo is the
        # store when R2 is off). Do NOT mkdir the cache root; that's what created the
        # in-repo generated_connectors/plan_cache/ folder.
        return

    from botocore.exceptions import ClientError

    client = _get_client()
    bucket = _get_bucket()
    if not bucket:
        logger.warning(
            "r2.ensure_bucket_skipped",
            reason="no bucket resolved — no request context?",
        )
        return

    coll = settings.R2_COLLECTION_PREFIX
    try:
        client.head_bucket(Bucket=bucket)
        logger.info("r2.bucket_accessible", bucket=bucket, collection=coll)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchBucket", "404", "NoSuchBucketError"):
            # Per-app bucket doesn't exist yet — create it now
            try:
                client.create_bucket(Bucket=bucket)
                logger.info("r2.bucket_created", bucket=bucket, collection=coll)
            except ClientError as create_err:
                create_code = create_err.response.get("Error", {}).get("Code", "")
                if create_code == "BucketAlreadyOwnedByYou":
                    # Race condition: another request created it first — that's fine
                    logger.info("r2.bucket_already_exists", bucket=bucket)
                else:
                    logger.error(
                        "r2.bucket_create_failed",
                        bucket=bucket,
                        error_code=create_code,
                        error=str(create_err),
                    )
                    raise
        else:
            logger.warning(
                "r2.bucket_check_failed",
                bucket=bucket,
                collection=coll,
                error_code=code,
            )


# ── Public async API ──────────────────────────────────────────────────


async def get_history(provider: str, service_slug: str, tenant_id: str) -> dict[str, Any] | None:
    """Check progress.json first — if plan_generated is True, return full cached plan.

    progress.json is the authoritative source of truth. If it says plan_generated=false
    or doesn't exist, returns None so the caller knows to run the LLM.

    Works with both R2 and local filesystem.

    Returns dict with: has_history, plan_generated, approval_made,
                       latest_prompt, date_executed, plan, plan_markdown.
    """
    if _use_local():
        return _local_get_history(provider, service_slug, tenant_id)

    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()

    try:
        # ── 1. Read progress.json — the primary truth ──────────────────
        progress_raw = await loop.run_in_executor(
            None,
            partial(
                _sync_read,
                client,
                bucket,
                _progress_key(provider, service_slug, tenant_id),
            ),
        )

        if not progress_raw:
            logger.info(
                "r2.no_progress",
                provider=provider,
                service_slug=service_slug,
                tenant_id=tenant_id,
            )
            return None

        progress = json.loads(progress_raw)

        if not progress.get("plan_generated", False):
            logger.info(
                "r2.plan_not_generated",
                provider=provider,
                service_slug=service_slug,
                tenant_id=tenant_id,
            )
            return None

        # ── 2. plan_generated=true — fetch plan.json, plan.md, prompts.csv in parallel ──
        plan_raw, md_raw, csv_raw = await asyncio.gather(
            loop.run_in_executor(
                None,
                partial(
                    _sync_read,
                    client,
                    bucket,
                    _plan_json_key(provider, service_slug, tenant_id),
                ),
            ),
            loop.run_in_executor(
                None,
                partial(
                    _sync_read,
                    client,
                    bucket,
                    _plan_md_key(provider, service_slug, tenant_id),
                ),
            ),
            loop.run_in_executor(
                None,
                partial(
                    _sync_read,
                    client,
                    bucket,
                    _csv_key(provider, service_slug, tenant_id),
                ),
            ),
        )

        plan = json.loads(plan_raw) if plan_raw else None

        latest_prompt = progress.get("latest_prompt", "")
        date_executed = progress.get("date_executed", "")

        # Fall back to CSV if progress.json doesn't carry the prompt
        if not latest_prompt and csv_raw:
            rows = list(csv.DictReader(io.StringIO(csv_raw)))
            if rows:
                latest_prompt = rows[-1].get("prompt", "")
                date_executed = rows[-1].get("date_executed", date_executed)

        logger.info(
            "r2.history_found",
            provider=provider,
            service_slug=service_slug,
            tenant_id=tenant_id,
            approval_made=progress.get("approval_made", "pending"),
        )

        return {
            "has_history": True,
            "plan_generated": True,
            "approval_made": progress.get("approval_made", "pending"),
            "latest_prompt": latest_prompt,
            "date_executed": date_executed,
            "plan": plan,
            "plan_markdown": md_raw or "",
            "guidelines_version": progress.get("guidelines_version", "unknown"),
        }

    except Exception as exc:
        logger.warning(
            "r2.get_history_failed",
            provider=provider,
            service_slug=service_slug,
            tenant_id=tenant_id,
            error=str(exc),
        )
        return None


def _local_get_history(provider: str, service_slug: str, tenant_id: str) -> dict[str, Any] | None:
    """Local filesystem version of get_history."""
    try:
        progress_raw = _local_read(_progress_key(provider, service_slug, tenant_id))
        if not progress_raw:
            logger.info(
                "cache.local_no_progress",
                provider=provider,
                service_slug=service_slug,
                tenant_id=tenant_id,
            )
            return None

        progress = json.loads(progress_raw)
        if not progress.get("plan_generated", False):
            logger.info(
                "cache.local_plan_not_generated",
                provider=provider,
                service_slug=service_slug,
            )
            return None

        plan_raw = _local_read(_plan_json_key(provider, service_slug, tenant_id))
        md_raw = _local_read(_plan_md_key(provider, service_slug, tenant_id))
        csv_raw = _local_read(_csv_key(provider, service_slug, tenant_id))

        plan = json.loads(plan_raw) if plan_raw else None

        latest_prompt = progress.get("latest_prompt", "")
        date_executed = progress.get("date_executed", "")

        if not latest_prompt and csv_raw:
            rows = list(csv.DictReader(io.StringIO(csv_raw)))
            if rows:
                latest_prompt = rows[-1].get("prompt", "")
                date_executed = rows[-1].get("date_executed", date_executed)

        logger.info(
            "cache.local_history_found",
            provider=provider,
            service_slug=service_slug,
            tenant_id=tenant_id,
            approval_made=progress.get("approval_made", "pending"),
        )

        return {
            "has_history": True,
            "plan_generated": True,
            "approval_made": progress.get("approval_made", "pending"),
            "latest_prompt": latest_prompt,
            "date_executed": date_executed,
            "plan": plan,
            "plan_markdown": md_raw or "",
            "guidelines_version": progress.get("guidelines_version", "unknown"),
        }

    except Exception as exc:
        logger.warning(
            "cache.local_get_history_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )
        return None


async def get_plan_markdown(provider: str, service_slug: str, tenant_id: str) -> str | None:
    """Fetch only the plan.md. Works with R2 or local filesystem."""
    if _use_local():
        return _local_read(_plan_md_key(provider, service_slug, tenant_id))

    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()

    try:
        return await loop.run_in_executor(
            None,
            partial(
                _sync_read,
                client,
                bucket,
                _plan_md_key(provider, service_slug, tenant_id),
            ),
        )
    except Exception as exc:
        logger.warning(
            "r2.get_md_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )
        return None


async def save_prompt_and_plan(
    provider: str,
    service_slug: str,
    tenant_id: str,
    prompt: str,
    plan_data: dict[str, Any],
    guidelines_version: str | None = None,
) -> None:
    """Save plan: overwrites prompts.csv, plan.json, plan.md, and progress.json.

    Uses R2 when configured, local filesystem otherwise.
    Ensures the bucket/directory exists before writing.
    Raises on write failure so the caller can emit accurate logs to the UI.
    """
    generated_at = datetime.now(UTC).isoformat()

    # ── 1. Build prompts.csv content ──
    csv_output = io.StringIO()
    csv_output.write("prompt,date_executed\n")
    row_buf = io.StringIO()
    csv.writer(row_buf).writerow([prompt, generated_at])
    csv_output.write(row_buf.getvalue())
    csv_content = csv_output.getvalue()

    # ── 2. Build plan.md ──
    md = _build_plan_markdown(provider, service_slug, prompt, plan_data, generated_at)

    # ── 3. Build plan.json ──
    plan_json = json.dumps(plan_data, indent=2, default=str)

    # Embed guidelines_version in plan_data so plan.json is self-describing
    if guidelines_version:
        plan_data = {**plan_data, "guidelines_version": guidelines_version}
        plan_json = json.dumps(plan_data, indent=2, default=str)

    # ── 4. Build progress.json — always read from disk first (source of truth) ──
    existing_raw = _local_read(_progress_key(provider, service_slug, tenant_id))
    existing_progress = json.loads(existing_raw) if existing_raw else {}

    progress = {
        "plan_generated": True,
        "approval_made": existing_progress.get("approval_made", "pending"),
        "latest_prompt": prompt,
        "date_executed": generated_at,
        "last_updated": generated_at,
        "provider": provider,
        "service_slug": service_slug,
        "tenant_id": tenant_id,
        "guidelines_version": guidelines_version or "unknown",
    }
    progress_json = json.dumps(progress, indent=2)

    # ── 5. Write to disk FIRST — disk is the source of truth ──
    _local_write(_csv_key(provider, service_slug, tenant_id), csv_content)
    _local_write(_plan_json_key(provider, service_slug, tenant_id), plan_json)
    _local_write(_plan_md_key(provider, service_slug, tenant_id), md)
    _local_write(_progress_key(provider, service_slug, tenant_id), progress_json)

    logger.info(
        "cache.disk_saved",
        provider=provider,
        service_slug=service_slug,
        tenant_id=tenant_id,
        md_bytes=len(md),
        json_bytes=len(plan_json),
        plan_generated=True,
    )

    # ── 6. Also write to R2 if configured ──
    if _use_local():
        return  # disk-only mode — done

    bucket = _get_bucket()
    logger.info("r2.upload_start", bucket=bucket, provider=provider, service_slug=service_slug)

    ensure_bucket()
    loop = asyncio.get_event_loop()
    client = _get_client()

    try:
        await asyncio.gather(
            loop.run_in_executor(
                None,
                partial(
                    _sync_write,
                    client,
                    bucket,
                    _csv_key(provider, service_slug, tenant_id),
                    csv_content,
                    "text/csv",
                ),
            ),
            loop.run_in_executor(
                None,
                partial(
                    _sync_write,
                    client,
                    bucket,
                    _plan_json_key(provider, service_slug, tenant_id),
                    plan_json,
                    "application/json",
                ),
            ),
            loop.run_in_executor(
                None,
                partial(
                    _sync_write,
                    client,
                    bucket,
                    _plan_md_key(provider, service_slug, tenant_id),
                    md,
                    "text/markdown",
                ),
            ),
            loop.run_in_executor(
                None,
                partial(
                    _sync_write,
                    client,
                    bucket,
                    _progress_key(provider, service_slug, tenant_id),
                    progress_json,
                    "application/json",
                ),
            ),
        )
    except Exception as exc:
        logger.error(
            "r2.upload_failed",
            bucket=bucket,
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )
        raise  # re-raise so callers can emit accurate logs / SSE warnings

    logger.info(
        "r2.saved",
        bucket=bucket,
        provider=provider,
        service_slug=service_slug,
        tenant_id=tenant_id,
        md_bytes=len(md),
        json_bytes=len(plan_json),
        plan_generated=True,
    )


def _local_save_prompt_and_plan(
    provider: str,
    service_slug: str,
    tenant_id: str,
    prompt: str,
    csv_content: str,
    plan_json: str,
    md: str,
    generated_at: str,
    guidelines_version: str | None = None,
) -> None:
    """Local filesystem version of save_prompt_and_plan."""
    # Read existing progress to preserve approval_made
    existing_raw = _local_read(_progress_key(provider, service_slug, tenant_id))
    existing_progress = json.loads(existing_raw) if existing_raw else {}

    progress = {
        "plan_generated": True,
        "approval_made": existing_progress.get("approval_made", "pending"),
        "latest_prompt": prompt,
        "date_executed": generated_at,
        "last_updated": generated_at,
        "provider": provider,
        "service_slug": service_slug,
        "tenant_id": tenant_id,
        "guidelines_version": guidelines_version or "unknown",
    }
    progress_json = json.dumps(progress, indent=2)

    _local_write(_csv_key(provider, service_slug, tenant_id), csv_content)
    _local_write(_plan_json_key(provider, service_slug, tenant_id), plan_json)
    _local_write(_plan_md_key(provider, service_slug, tenant_id), md)
    _local_write(_progress_key(provider, service_slug, tenant_id), progress_json)

    logger.info(
        "cache.local_saved",
        provider=provider,
        service_slug=service_slug,
        tenant_id=tenant_id,
        md_bytes=len(md),
        json_bytes=len(plan_json),
        plan_generated=True,
    )


# ── Guidelines staleness helpers ──────────────────────────────────────


async def get_cached_guidelines_version(provider: str, service_slug: str, tenant_id: str) -> str | None:
    """Return the guidelines_version stored in progress.json, or None if not found."""
    if _use_local():
        raw = _local_read(_progress_key(provider, service_slug, tenant_id))
        if not raw:
            return None
        progress = json.loads(raw)
        return progress.get("guidelines_version")

    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()
    try:
        raw = await loop.run_in_executor(
            None,
            partial(
                _sync_read,
                client,
                bucket,
                _progress_key(provider, service_slug, tenant_id),
            ),
        )
        if not raw:
            return None
        return json.loads(raw).get("guidelines_version")
    except Exception:
        return None


async def invalidate_stale_plan(provider: str, service_slug: str, tenant_id: str) -> None:
    """Reset plan_generated=False in progress.json so the next call forces LLM regeneration.

    Used when guidelines have been updated after the plan was cached.
    """
    key = _progress_key(provider, service_slug, tenant_id)

    # ── Read from disk first — source of truth ──
    raw = _local_read(key)
    if not raw:
        return
    progress = json.loads(raw)
    progress["plan_generated"] = False
    progress["stale_reason"] = "guidelines_updated"
    updated_json = json.dumps(progress, indent=2)

    # ── Write to disk FIRST ──
    try:
        _local_write(key, updated_json)
        logger.info(
            "cache.disk_plan_invalidated",
            provider=provider,
            service_slug=service_slug,
            tenant_id=tenant_id,
        )
    except Exception as exc:
        logger.warning("cache.disk_plan_invalidate_failed", error=str(exc))

    # ── Also write to R2 if configured ──
    if _use_local():
        return

    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()
    try:
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, bucket, key, updated_json, "application/json"),
        )
        logger.info(
            "r2.plan_invalidated",
            provider=provider,
            service_slug=service_slug,
            tenant_id=tenant_id,
        )
    except Exception as exc:
        logger.warning("r2.plan_invalidate_failed", error=str(exc))


# ── Execution state ───────────────────────────────────────────────────


def _execution_state_key(provider: str, service_slug: str, tenant_id: str = "") -> str:
    # tenant_id removed — bucket is already tenant-scoped
    return _k(_coll(), provider, service_slug, "execution_state.json")


async def get_execution_state(provider: str, service_slug: str, tenant_id: str) -> dict[str, Any] | None:
    """Read execution_state.json. Works with R2 or local filesystem."""
    if _use_local():
        raw = _local_read(_execution_state_key(provider, service_slug, tenant_id))
        return json.loads(raw) if raw else None

    loop = asyncio.get_event_loop()
    client = _get_client()
    try:
        raw = await loop.run_in_executor(
            None,
            partial(
                _sync_read,
                client,
                _get_bucket(),
                _execution_state_key(provider, service_slug, tenant_id),
            ),
        )
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning(
            "r2.get_execution_state_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )
        return None


async def save_execution_state(
    provider: str,
    service_slug: str,
    tenant_id: str,
    completed_steps: list[str],
    session_id: str,
) -> None:
    """Write execution_state.json. Works with R2 or local filesystem."""
    state = {
        "tenant_id": tenant_id,
        "provider": provider,
        "service_slug": service_slug,
        "completed_steps": completed_steps,
        "last_session_id": session_id,
        "last_updated": datetime.now(UTC).isoformat(),
    }
    state_json = json.dumps(state, indent=2)

    key = _execution_state_key(provider, service_slug, tenant_id)

    # Write to disk FIRST — source of truth
    try:
        _local_write(key, state_json)
        logger.info(
            "cache.disk_execution_state_saved",
            provider=provider,
            service_slug=service_slug,
            completed=completed_steps,
        )
    except Exception as exc:
        logger.warning(
            "cache.disk_save_execution_state_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )

    # Also write to R2 if configured
    if _use_local():
        return

    loop = asyncio.get_event_loop()
    client = _get_client()
    try:
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, _get_bucket(), key, state_json, "application/json"),
        )
        logger.info(
            "r2.execution_state_saved",
            provider=provider,
            service_slug=service_slug,
            completed=completed_steps,
        )
    except Exception as exc:
        logger.warning(
            "r2.save_execution_state_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )


async def get_stepper_max_step(
    provider: str,
    service_slug: str,
    tenant_id: str,
) -> int:
    """Read stepper_max_step from progress.json.  Returns 0 if not set or file absent."""
    key = _progress_key(provider, service_slug, tenant_id)
    try:
        existing_raw = _local_read(key)
        if existing_raw:
            progress = json.loads(existing_raw)
            val = progress.get("stepper_max_step", 0)
            if isinstance(val, int):
                return val
    except Exception as exc:
        logger.warning("cache.get_stepper_max_step_failed", error=str(exc))

    if _use_local():
        return 0

    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()
    try:
        raw = await loop.run_in_executor(None, partial(_sync_read, client, bucket, key))
        if raw:
            progress = json.loads(raw)
            val = progress.get("stepper_max_step", 0)
            if isinstance(val, int):
                return val
    except Exception as exc:
        logger.warning("r2.get_stepper_max_step_failed", error=str(exc))
    return 0


async def update_stepper_max_step(
    provider: str,
    service_slug: str,
    tenant_id: str,
    step_index: int,
) -> None:
    """Update stepper_max_step in progress.json (disk first, then R2).

    Only writes if the new value is greater than the stored one (monotonically increasing).
    """
    key = _progress_key(provider, service_slug, tenant_id)

    # ── Read + update on disk FIRST ──────────────────────────────────────
    try:
        existing_raw = _local_read(key)
        progress = json.loads(existing_raw) if existing_raw else {}
        existing_max = progress.get("stepper_max_step", 0)
        if step_index <= existing_max:
            # Nothing to update on disk — R2 also already has a higher or equal value
            return
        progress["stepper_max_step"] = step_index
        progress["last_updated"] = datetime.now(UTC).isoformat()
        updated_json = json.dumps(progress, indent=2)
        _local_write(key, updated_json)
        logger.info(
            "cache.disk_stepper_max_step_updated",
            provider=provider,
            service_slug=service_slug,
            step_index=step_index,
        )
    except Exception as exc:
        logger.warning("cache.disk_stepper_max_step_update_failed", error=str(exc))
        updated_json = json.dumps(
            {
                "stepper_max_step": step_index,
                "last_updated": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )

    # ── Also write to R2 if configured ──────────────────────────────────
    if _use_local():
        return

    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()
    try:
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, bucket, key, updated_json, "application/json"),
        )
        logger.info(
            "r2.stepper_max_step_updated",
            provider=provider,
            service_slug=service_slug,
            step_index=step_index,
        )
    except Exception as exc:
        logger.warning("r2.stepper_max_step_update_failed", error=str(exc))


async def update_approval_status(
    provider: str,
    service_slug: str,
    tenant_id: str,
    status: str,  # "pending" | "approved"
) -> None:
    """Update approval_made in progress.json. Called when user approves a plan."""
    key = _progress_key(provider, service_slug, tenant_id)

    # ── Read + update on disk FIRST — source of truth ──
    try:
        existing_raw = _local_read(key)
        progress = json.loads(existing_raw) if existing_raw else {}
        progress["approval_made"] = status
        progress["last_updated"] = datetime.now(UTC).isoformat()
        updated_json = json.dumps(progress, indent=2)
        _local_write(key, updated_json)
        logger.info(
            "cache.disk_approval_updated",
            provider=provider,
            service_slug=service_slug,
            tenant_id=tenant_id,
            status=status,
        )
    except Exception as exc:
        logger.warning("cache.disk_approval_update_failed", error=str(exc))
        updated_json = json.dumps(
            {
                "approval_made": status,
                "last_updated": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )

    # ── Also write to R2 if configured ──
    if _use_local():
        return

    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()
    try:
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, bucket, key, updated_json, "application/json"),
        )
        logger.info(
            "r2.approval_updated",
            provider=provider,
            service_slug=service_slug,
            tenant_id=tenant_id,
            status=status,
        )
    except Exception as exc:
        logger.warning("r2.approval_update_failed", error=str(exc))


# ── Cache purge (called on session delete) ────────────────────────────


async def clear_execution_state(provider: str, service_slug: str, tenant_id: str) -> None:
    """Delete only execution_state.json — preserves plan.json, progress.json, plan.md.

    Called on re-execute so the plan and approval status are NOT wiped.
    Works with both R2 and local filesystem.
    """
    key = _execution_state_key(provider, service_slug, tenant_id)

    if _use_local():
        path = _local_path(key)
        if path.exists():
            try:
                path.unlink()
            except Exception as exc:
                logger.warning("cache.local_execution_state_clear_failed", error=str(exc))
        return

    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()
    try:
        await loop.run_in_executor(None, partial(client.delete_object, Bucket=bucket, Key=key))
        logger.info("r2.execution_state_cleared", provider=provider, service_slug=service_slug)
    except Exception as exc:
        logger.warning("r2.execution_state_clear_failed", error=str(exc))


async def clear_cache(provider: str, service_slug: str, tenant_id: str) -> None:
    """Delete all cached plan/execution/failure files for a provider/service_slug/tenant.

    Called when a connector session is DELETED so a fresh run starts from scratch.
    Do NOT call this on re-execute — use clear_execution_state() instead to preserve the plan.
    Works with both R2 and local filesystem.
    """
    # All known keys for this provider/service_slug/tenant
    keys = [
        _csv_key(provider, service_slug, tenant_id),
        _plan_json_key(provider, service_slug, tenant_id),
        _plan_md_key(provider, service_slug, tenant_id),
        _progress_key(provider, service_slug, tenant_id),
        _execution_state_key(provider, service_slug, tenant_id),
    ]

    if _use_local():
        removed = []
        for key in keys:
            path = _local_path(key)
            if path.exists():
                try:
                    path.unlink()
                    removed.append(key)
                except Exception as exc:
                    logger.warning("cache.local_clear_failed", key=key, error=str(exc))

        # Also remove the failures/ subdirectory
        failures_dir = _local_path(_k(_coll(), provider, service_slug, "failures"))
        if failures_dir.exists() and failures_dir.is_dir():
            import shutil

            try:
                shutil.rmtree(str(failures_dir))
                removed.append(f"{provider}/{service_slug}/failures/")
            except Exception as exc:
                logger.warning("cache.local_failures_clear_failed", error=str(exc))

        logger.info(
            "cache.local_cleared",
            provider=provider,
            service_slug=service_slug,
            tenant_id=tenant_id,
            removed=removed,
        )
        return

    # R2 path
    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()

    async def _delete_key(key: str) -> None:
        try:
            await loop.run_in_executor(None, partial(client.delete_object, Bucket=bucket, Key=key))
        except Exception as exc:
            logger.warning("r2.clear_key_failed", key=key, error=str(exc))

    # Delete all known flat files
    await asyncio.gather(*[_delete_key(k) for k in keys])

    # List and delete all failures/ objects under this prefix
    failures_prefix = _k(_coll(), provider, service_slug, "failures") + "/"
    try:
        resp = await loop.run_in_executor(
            None,
            partial(client.list_objects_v2, Bucket=bucket, Prefix=failures_prefix),
        )
        failure_keys = [obj["Key"] for obj in resp.get("Contents", [])]
        if failure_keys:
            await asyncio.gather(*[_delete_key(k) for k in failure_keys])
    except Exception as exc:
        logger.warning("r2.clear_failures_list_failed", error=str(exc))

    logger.info(
        "r2.cache_cleared",
        provider=provider,
        service_slug=service_slug,
        tenant_id=tenant_id,
    )


# ── Step Prompts — R2-backed versioned LLM prompt storage ────────────────────
#
# Prompts live under {R2_COLLECTION_PREFIX}/STEP_PROMPTS/ so they can be
# updated at runtime without a code deployment.
# R2 is the single source of truth; the Python constant is fallback-only.
#
# Key layout:  {collection}/STEP_PROMPTS/{prompt_name}.txt
#
# Supported prompt names (match constants in codegen_prompt.py / agentic_fix.py):
#   CONNECTOR_SYSTEM_PROMPT, TEST_SYSTEM_PROMPT, FIX_CODE_PROMPT,
#   FIX_TESTS_PROMPT, FIX_CONNECTOR_FOR_TESTS_PROMPT,
#   TEST_RULES_GENERATION_PROMPT, MODULE_FILE_SYSTEM_PROMPT,
#   TEST_MODULE_SYSTEM_PROMPT, USER_MODIFY_PROMPT, USER_RESTRUCTURE_PROMPT,
#   CONNECTOR_GEN_SYSTEM, METADATA_GEN_SYSTEM, DOCS_GEN_SYSTEM, FIX_SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

_STEP_PROMPTS_PREFIX = "STEP_PROMPTS"
_LOCAL_STEP_PROMPTS_DIR = _LOCAL_CACHE_DIR / _STEP_PROMPTS_PREFIX

# In-process cache so we only hit R2/disk once per process lifetime per prompt.
# Cleared on service restart — that's intentional so a prompt update takes effect
# after a rolling restart without code changes.
_step_prompt_cache: dict[str, str] = {}


def _step_prompt_key(prompt_name: str) -> str:
    # Step prompts always live in the shared bucket — always use the full collection prefix
    # regardless of the current per-app bucket context.
    return f"{settings.R2_COLLECTION_PREFIX}/{_STEP_PROMPTS_PREFIX}/{prompt_name}.txt"


async def _load_raw_step_prompt(prompt_name: str, local_fallback: str) -> str:
    """Low-level loader: R2 → local disk → local_fallback (no in-process cache).

    Seeds local disk on first fallback use so the next process restart reads from
    disk instead of the hardcoded string.
    """
    key = _step_prompt_key(prompt_name)

    if _use_local():
        local_path = _local_path(key)
        if local_path.exists():
            logger.debug("step_prompt.loaded_local", prompt=prompt_name)
            return local_path.read_text(encoding="utf-8")
    else:
        try:
            loop = asyncio.get_event_loop()
            client = _get_client()
            # Step prompts live in the shared bucket (shielvasense-integration-plans),
            # NOT in the per-app bucket — they are global admin-managed resources.
            bucket = _get_shared_bucket()
            raw = await loop.run_in_executor(None, partial(_sync_read, client, bucket, key))
            if raw:
                logger.debug("step_prompt.loaded_r2", prompt=prompt_name, bucket=bucket)
                return raw
        except Exception as exc:
            logger.warning("step_prompt.r2_read_failed", prompt=prompt_name, error=str(exc))

    # Local-disk seeding disabled (no disk writes — that created the in-repo
    # plan_cache folder). Reads fall back to the caller's hardcoded prompt.
    logger.debug("step_prompt.using_local_fallback", prompt=prompt_name)
    return local_fallback


async def get_step_prompt(
    prompt_name: str,
    local_fallback: str,
    *,
    auth_type: str | None = None,
) -> str:
    """Return the live step prompt from R2 (or local cache), falling back to local_fallback.

    Priority:
      1. In-process memory cache (hot path — zero I/O)
      2. R2 object at STEP_PROMPTS/{prompt_name}.txt
      3. Local filesystem at plan_cache/STEP_PROMPTS/{prompt_name}.txt
      4. local_fallback (the hardcoded string in the caller)

    Auth-type overlays
    ─────────────────
    When auth_type is provided (e.g. "oauth2_code", "api_key", "service_account")
    the function also tries to load an auth-specific addendum from
    STEP_PROMPTS/{prompt_name}_{auth_type}.txt and appends it to the base prompt
    under a clearly labelled section header.

    This lets operators update per-auth rules in R2 without a code deploy and
    without duplicating the entire base prompt.  If no addendum file exists for
    the given auth_type the base prompt is returned unchanged.
    """
    cache_key = f"{prompt_name}::{auth_type}" if auth_type else prompt_name
    if cache_key in _step_prompt_cache:
        return _step_prompt_cache[cache_key]

    # Load base prompt
    base = await _load_raw_step_prompt(prompt_name, local_fallback)

    if not auth_type:
        _step_prompt_cache[cache_key] = base
        return base

    # Try to load auth-type addendum — no hard fallback, missing = silently skip
    addendum_name = f"{prompt_name}_{auth_type}"
    addendum = await _load_raw_step_prompt(addendum_name, "")

    if addendum:
        composed = f"{base}\n\n## Auth-Type Specific Rules — {auth_type}\n\n{addendum}"
        logger.debug("step_prompt.auth_overlay_applied", prompt=prompt_name, auth_type=auth_type)
    else:
        composed = base
        logger.debug("step_prompt.auth_overlay_missing", prompt=prompt_name, auth_type=auth_type)

    _step_prompt_cache[cache_key] = composed
    return composed


async def save_step_prompt(prompt_name: str, content: str) -> None:
    """Write a step prompt to R2 (or local cache) and invalidate the in-process cache.

    Call this when you want to update a prompt without a code deployment.
    Invalidates both the bare cache entry and all auth-type composed variants
    (keys of the form "{prompt_name}::*") so the next request rebuilds them.
    """
    key = _step_prompt_key(prompt_name)
    # Invalidate bare entry + all auth-type composed variants
    for k in [k for k in _step_prompt_cache if k == prompt_name or k.startswith(f"{prompt_name}::")]:
        _step_prompt_cache.pop(k, None)

    if _use_local():
        # Local-disk fallback disabled — no-op when R2 is off (admin prompt updates
        # require R2; reads use the caller's hardcoded default).
        logger.info("step_prompt.save_skipped_no_r2", prompt=prompt_name)
        return

    loop = asyncio.get_event_loop()
    client = _get_client()
    # Step prompts are saved to the shared bucket — admin-managed, global resource.
    bucket = _get_shared_bucket()
    await loop.run_in_executor(
        None,
        partial(_sync_write, client, bucket, key, content, "text/plain"),
    )
    logger.info("step_prompt.saved_r2", prompt=prompt_name, bucket=bucket, chars=len(content))


async def sync_all_step_prompts_to_r2() -> dict[str, str]:
    """Upload all hardcoded prompts from codegen_prompt.py to R2 (or local cache).

    Writes a prompt when:
      - it doesn't exist in R2 yet (first upload), OR
      - the stored content differs from the local constant (code was updated)

    This ensures that when a prompt constant is updated in code, the R2-cached
    version is automatically replaced on next service startup — without requiring
    manual deletion of R2 keys.

    Returns a dict of {prompt_name: "uploaded" | "skipped" | "error"}.
    """
    from integration.prompts.codegen_prompt import (
        CONNECTOR_SYSTEM_PROMPT,
        FIX_CODE_PROMPT,
        FIX_CONNECTOR_FOR_TESTS_PROMPT,
        FIX_TESTS_PROMPT,
        INTEGRATION_TEST_SYSTEM_PROMPT,
        MODULE_FILE_SYSTEM_PROMPT,
        TEST_MODULE_SYSTEM_PROMPT,
        TEST_RULES_GENERATION_PROMPT,
        TEST_SYSTEM_PROMPT,
        USER_MODIFY_PROMPT,
        USER_RESTRUCTURE_PROMPT,
    )
    from integration.prompts.docs_prompt import (
        DOCS_GENERATION_PROMPT,
        DOCS_UPDATE_PROMPT,
    )
    from integration.prompts.planning_prompt import (
        PLANNING_SYSTEM_PROMPT,
        REPLAN_SYSTEM_PROMPT,
    )
    from integration.services.agentic_fix import (
        _AUTH_TYPE_ADDENDA,
        _CONNECTOR_FIX_SYSTEM,
        _CONNECTOR_GEN_SYSTEM,
        _DOCS_GEN_SYSTEM,
        _FIX_SYSTEM,
        _METADATA_GEN_SYSTEM,
        _TEST_GEN_SYSTEM,
    )
    from integration.services.code_analysis_service import _ANALYSIS_SYSTEM
    from integration.services.docs_synth_service import (
        _EXTRACTION_PROMPT,
        _SYNTHESIS_PROMPT,
    )
    from integration.services.step_executor import (
        _METADATA_SYSTEM_PROMPT,
        _SETUP_INSTRUCTIONS_SYSTEM,
        _TEST_GUIDELINES_SYSTEM,
    )

    prompts = {
        "CONNECTOR_SYSTEM_PROMPT": CONNECTOR_SYSTEM_PROMPT,
        "TEST_SYSTEM_PROMPT": TEST_SYSTEM_PROMPT,
        "INTEGRATION_TEST_SYSTEM_PROMPT": INTEGRATION_TEST_SYSTEM_PROMPT,
        "FIX_CODE_PROMPT": FIX_CODE_PROMPT,
        "FIX_TESTS_PROMPT": FIX_TESTS_PROMPT,
        "FIX_CONNECTOR_FOR_TESTS_PROMPT": FIX_CONNECTOR_FOR_TESTS_PROMPT,
        "TEST_RULES_GENERATION_PROMPT": TEST_RULES_GENERATION_PROMPT,
        "MODULE_FILE_SYSTEM_PROMPT": MODULE_FILE_SYSTEM_PROMPT,
        "TEST_MODULE_SYSTEM_PROMPT": TEST_MODULE_SYSTEM_PROMPT,
        "USER_MODIFY_PROMPT": USER_MODIFY_PROMPT,
        "USER_RESTRUCTURE_PROMPT": USER_RESTRUCTURE_PROMPT,
        "METADATA_SYSTEM_PROMPT": _METADATA_SYSTEM_PROMPT,
        # Agentic prompts (Gemini loop system prompts)
        "CONNECTOR_GEN_SYSTEM": _CONNECTOR_GEN_SYSTEM,
        "METADATA_GEN_SYSTEM": _METADATA_GEN_SYSTEM,
        "DOCS_GEN_SYSTEM": _DOCS_GEN_SYSTEM,
        "FIX_SYSTEM": _FIX_SYSTEM,
        # Setup instructions — connector-specific credential guide
        "SETUP_INSTRUCTIONS_SYSTEM": _SETUP_INSTRUCTIONS_SYSTEM,
        # Test guidelines — connector-specific pytest guideline document
        "TEST_GUIDELINES_SYSTEM": _TEST_GUIDELINES_SYSTEM,
        # Planning prompts
        "PLANNING_SYSTEM_PROMPT": PLANNING_SYSTEM_PROMPT,
        "REPLAN_SYSTEM_PROMPT": REPLAN_SYSTEM_PROMPT,
        # Docs prompts
        "DOCS_GENERATION_PROMPT": DOCS_GENERATION_PROMPT,
        "DOCS_UPDATE_PROMPT": DOCS_UPDATE_PROMPT,
        # Catalog AI suggestions (inlined to avoid circular import from catalog_routes)
        "SUGGEST_SERVICES_SYSTEM": """You are an expert software integration architect.
Given a provider/platform name and description, suggest realistic API services that this provider would offer.
Return ONLY a valid JSON array — no markdown, no explanation.
Each service object must have:
  service_key (snake_case slug, unique within the list),
  display_name (short human label),
  description (1-2 sentence what the service does),
  auth_type (one of: oauth2, api_key, bearer_token, basic, service_account),
  category (one of: productivity, storage, communication, payments, crm, data, cloud, analytics, identity, social, iot, maps, general),
  suggested_sdk (the main Python PyPI package name, if known, otherwise empty string).
Suggest 6–12 services that make practical sense for this provider.""",
        "SUGGEST_DEPS_SYSTEM": """You are a Python dependency expert for API integrations.
Given a provider name and a list of its services (with auth types), suggest the Python PyPI packages needed to build connectors.
Return ONLY a valid JSON object — no markdown, no explanation.
Format:
{
  "packages": [
    {"name": "package-name", "version": ">=x.y", "reason": "why it is needed"}
  ]
}
Include: HTTP clients (httpx or requests), auth helpers (authlib, google-auth, etc.), official SDKs if available.
Limit to 4–8 packages. Prefer widely-used, well-maintained packages.""",
        # Code analysis
        "ANALYSIS_SYSTEM": _ANALYSIS_SYSTEM,
        # Docs synthesis
        "SYNTHESIS_PROMPT": _SYNTHESIS_PROMPT,
        "EXTRACTION_PROMPT": _EXTRACTION_PROMPT,
        # Test generation
        "TEST_GEN_SYSTEM": _TEST_GEN_SYSTEM,
        # Connector fix
        "CONNECTOR_FIX_SYSTEM": _CONNECTOR_FIX_SYSTEM,
    }

    # Auth-type specific addenda: CONNECTOR_GEN_SYSTEM_{auth_type}
    for auth_type, addendum in _AUTH_TYPE_ADDENDA.items():
        prompts[f"CONNECTOR_GEN_SYSTEM_{auth_type}"] = addendum

    results: dict[str, str] = {}

    for name, content in prompts.items():
        key = _step_prompt_key(name)
        try:
            # Check if it already exists — don't overwrite manual edits
            existing = None
            if _use_local():
                lp = _local_path(key)
                existing = lp.read_text(encoding="utf-8") if lp.exists() else None
            else:
                loop = asyncio.get_event_loop()
                client = _get_client()
                # Read from shared bucket — step prompts are global admin resources
                existing = await loop.run_in_executor(None, partial(_sync_read, client, _get_shared_bucket(), key))

            if existing and existing.strip() == content.strip():
                results[name] = "skipped"
                logger.debug("step_prompt.sync_skipped", prompt=name)
            else:
                # Upload if new OR if content has changed since last sync
                if existing:
                    logger.info(
                        "step_prompt.sync_updated",
                        prompt=name,
                        reason="content changed — overwriting stale R2 version",
                    )
                await save_step_prompt(name, content)
                results[name] = "uploaded" if not existing else "updated"
        except Exception as exc:
            results[name] = f"error: {exc}"
            logger.warning("step_prompt.sync_failed", prompt=name, error=str(exc))

    logger.info("step_prompt.sync_complete", results=results)
    return results


# ── Connector documentation storage ─────────────────────────────────
# Key pattern: {R2_COLLECTION_PREFIX}/CONNECTOR_DOCS/{provider}/{service_slug}/docs.json
# The bucket itself is already tenant-scoped (via _get_bucket()), so tenant_id
# is NOT included in the key — matches the same convention as plan.json, progress.json, etc.
# Stored as JSON. Falls back to local filesystem when R2 not configured.

_DOCS_PREFIX = "CONNECTOR_DOCS"


def _docs_key(tenant_id: str, provider: str, service_slug: str) -> str:
    # tenant_id intentionally omitted from path — bucket is tenant-scoped
    return _k(_coll(), _DOCS_PREFIX, provider, service_slug, "docs.json")


async def get_connector_docs(tenant_id: str, provider: str, service_slug: str) -> dict | None:
    """Load connector docs JSON from R2 (or local). Returns None if not found."""
    key = _docs_key(tenant_id, provider, service_slug)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            raw = await loop.run_in_executor(None, _local_read, key)
        else:
            client = _get_client()
            raw = await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("connector_docs.get_failed", key=key, error=str(exc))
    return None


async def save_connector_docs(tenant_id: str, provider: str, service_slug: str, docs: dict) -> None:
    """Persist connector docs JSON to R2 (or local). Overwrites any existing docs."""
    key = _docs_key(tenant_id, provider, service_slug)
    content = json.dumps(docs, ensure_ascii=False, indent=2)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            await loop.run_in_executor(None, partial(_local_write, key, content))
        else:
            client = _get_client()
            await loop.run_in_executor(
                None,
                partial(
                    _sync_write,
                    client,
                    _get_bucket(),
                    key,
                    content,
                    "application/json",
                ),
            )
        logger.info(
            "connector_docs.saved",
            tenant_id=tenant_id,
            provider=provider,
            service_slug=service_slug,
        )
    except Exception as exc:
        logger.warning("connector_docs.save_failed", key=key, error=str(exc))


# ── Test Guidelines storage ───────────────────────────────────────────
# Key pattern: {collection}/{provider}/{service_slug}/test_guidelines.md
# Stored as markdown. Falls back to local filesystem when R2 not configured.


def _implementation_plan_key(provider: str, service_slug: str) -> str:
    """R2 key: {collection}/{provider}/{service_slug}/implementation_plan.md"""
    return _k(_coll(), provider, service_slug, "implementation_plan.md")


async def get_implementation_plan(provider: str, service_slug: str) -> str | None:
    """Fetch connector-specific implementation plan — disk first, then R2 as fallback."""
    key = _implementation_plan_key(provider, service_slug)
    # Always try disk first — source of truth
    disk_content = _local_read(key)
    if disk_content:
        return disk_content
    # Fall back to R2 if configured
    if _use_local():
        return None
    loop = asyncio.get_event_loop()
    try:
        client = _get_client()
        content = await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
        # Seed disk from R2 for future reads
        if content:
            with contextlib.suppress(Exception):
                _local_write(key, content)
        return content
    except Exception as exc:
        logger.warning(
            "implementation_plan.get_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )
        return None


async def store_implementation_plan(provider: str, service_slug: str, content: str) -> None:
    """Store connector-specific implementation plan — disk first, then R2."""
    key = _implementation_plan_key(provider, service_slug)
    loop = asyncio.get_event_loop()

    # Write to disk FIRST — source of truth
    await loop.run_in_executor(None, partial(_local_write, key, content))
    logger.info(
        "implementation_plan.saved_disk",
        provider=provider,
        service_slug=service_slug,
        chars=len(content),
    )

    # Also write to R2 if configured
    if _use_local():
        return
    try:
        client = _get_client()
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, _get_bucket(), key, content, "text/markdown"),
        )
        logger.info(
            "implementation_plan.saved_r2",
            provider=provider,
            service_slug=service_slug,
            chars=len(content),
        )
    except Exception as exc:
        logger.warning(
            "implementation_plan.save_r2_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )
        raise


def _test_guidelines_key(provider: str, service_slug: str) -> str:
    """R2 key: {collection}/{provider}/{service_slug}/test_guidelines.md"""
    return _k(_coll(), provider, service_slug, "test_guidelines.md")


async def get_test_guidelines(provider: str, service_slug: str) -> str | None:
    """Fetch connector-specific test guidelines — disk first, then R2 as fallback."""
    key = _test_guidelines_key(provider, service_slug)
    # Always try disk first — source of truth
    disk_content = _local_read(key)
    if disk_content:
        return disk_content
    # Fall back to R2 if configured
    if _use_local():
        return None
    loop = asyncio.get_event_loop()
    try:
        client = _get_client()
        content = await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
        # Seed disk from R2 for future reads
        if content:
            with contextlib.suppress(Exception):
                _local_write(key, content)
        return content
    except Exception as exc:
        logger.warning(
            "test_guidelines.get_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )
        return None


async def store_test_guidelines(provider: str, service_slug: str, content: str) -> None:
    """Store connector-specific test guidelines — disk first, then R2."""
    key = _test_guidelines_key(provider, service_slug)
    loop = asyncio.get_event_loop()

    # Write to disk FIRST — source of truth
    await loop.run_in_executor(None, partial(_local_write, key, content))
    logger.info(
        "test_guidelines.saved_disk",
        provider=provider,
        service_slug=service_slug,
        chars=len(content),
    )

    # Also write to R2 if configured
    if _use_local():
        return
    try:
        client = _get_client()
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, _get_bucket(), key, content, "text/markdown"),
        )
        logger.info(
            "test_guidelines.saved_r2",
            provider=provider,
            service_slug=service_slug,
            chars=len(content),
        )
    except Exception as exc:
        logger.warning(
            "test_guidelines.save_r2_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )
        raise


def _setup_instructions_key(provider: str, service_slug: str) -> str:
    """R2 key: {collection}/{provider}/{service_slug}/setup_instructions.md"""
    return _k(_coll(), provider, service_slug, "setup_instructions.md")


async def get_setup_instructions(provider: str, service_slug: str) -> str | None:
    """Fetch connector-specific setup instructions — disk first, then R2 as fallback."""
    key = _setup_instructions_key(provider, service_slug)
    # Always try disk first
    disk_content = _local_read(key)
    if disk_content:
        return disk_content
    # Fall back to R2 if configured
    if _use_local():
        return None
    loop = asyncio.get_event_loop()
    try:
        client = _get_client()
        content = await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
        # Seed disk from R2 for future reads
        if content:
            with contextlib.suppress(Exception):
                _local_write(key, content)
        return content
    except Exception as exc:
        logger.warning(
            "setup_instructions.get_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )
        return None


async def store_setup_instructions(provider: str, service_slug: str, content: str) -> None:
    """Store connector-specific setup instructions — disk first, then R2."""
    key = _setup_instructions_key(provider, service_slug)
    loop = asyncio.get_event_loop()

    # Write to disk FIRST — source of truth
    try:
        await loop.run_in_executor(None, partial(_local_write, key, content))
        logger.info(
            "setup_instructions.saved_disk",
            provider=provider,
            service_slug=service_slug,
            chars=len(content),
        )
    except Exception as exc:
        logger.warning(
            "setup_instructions.disk_save_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )

    # Also write to R2 if configured
    if _use_local():
        return
    try:
        client = _get_client()
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, _get_bucket(), key, content, "text/markdown"),
        )
        logger.info(
            "setup_instructions.saved_r2",
            provider=provider,
            service_slug=service_slug,
            chars=len(content),
        )
    except Exception as exc:
        logger.warning(
            "setup_instructions.save_r2_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )


def _entity_builder_key(provider: str, service_slug: str, method_name: str) -> str:
    """R2 key: {collection}/{provider}/{service_slug}/entity_builder_{method_name}.json"""
    return _k(_coll(), provider, service_slug, f"entity_builder_{method_name}.json")


async def store_entity_builder_config(provider: str, service_slug: str, method_name: str, config: dict) -> None:
    """Persist entity builder config (entity + mappings + method) to R2 for use in implement_persistence step."""
    import json as _json

    key = _entity_builder_key(provider, service_slug, method_name)
    content = _json.dumps(config, indent=2, default=str)
    loop = asyncio.get_event_loop()

    # ── Write to disk FIRST — source of truth ──
    try:
        await loop.run_in_executor(None, partial(_local_write, key, content))
        logger.info(
            "entity_builder.saved_disk",
            provider=provider,
            service_slug=service_slug,
            method=method_name,
        )
    except Exception as exc:
        logger.warning("entity_builder.disk_save_failed", error=str(exc))

    # ── Also write to R2 if configured ──
    if _use_local():
        return
    try:
        client = _get_client()
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, _get_bucket(), key, content, "application/json"),
        )
        logger.info(
            "entity_builder.saved_r2",
            provider=provider,
            service_slug=service_slug,
            method=method_name,
        )
    except Exception as exc:
        logger.warning("entity_builder.save_r2_failed", error=str(exc))
        raise


async def get_entity_builder_config(provider: str, service_slug: str, method_name: str) -> dict | None:
    """Fetch entity builder config from R2. Returns None if not found."""
    import json as _json

    key = _entity_builder_key(provider, service_slug, method_name)
    loop = asyncio.get_event_loop()
    if _use_local():
        raw = await loop.run_in_executor(None, _local_read, key)
        return _json.loads(raw) if raw else None
    try:
        client = _get_client()
        raw = await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
        return _json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning(
            "entity_builder.get_failed",
            provider=provider,
            service_slug=service_slug,
            method=method_name,
            error=str(exc),
        )
        return None


async def delete_connector_session_files(tenant_id: str, service_slug: str, session_id: str) -> int:
    """Delete all connector code files for a session from R2 (or local cache).

    Called on session delete so generated connector code doesn't accumulate in R2.
    Returns the number of files deleted.
    """
    prefix = connector_session_r2_prefix(tenant_id, service_slug, session_id)
    loop = asyncio.get_event_loop()

    if _use_local():
        local_root = _local_path(prefix)
        if not local_root.exists():
            return 0
        count = 0
        import shutil as _shutil

        try:
            _shutil.rmtree(str(local_root))
            count = 1  # treat the whole dir as one deletion unit
            logger.info(
                "connector_code.local_session_deleted",
                tenant_id=tenant_id,
                service_slug=service_slug,
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning("connector_code.local_session_delete_failed", error=str(exc))
        return count

    try:
        client = _get_client()
        bucket = _get_bucket()

        def _list_and_delete() -> int:
            keys: list[str] = []
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix + "/"):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
            if not keys:
                return 0
            # boto3 batch delete (up to 1000 per call)
            for i in range(0, len(keys), 1000):
                batch = [{"Key": k} for k in keys[i : i + 1000]]
                client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
            return len(keys)

        deleted = await loop.run_in_executor(None, _list_and_delete)
        logger.info(
            "connector_code.r2_session_deleted",
            tenant_id=tenant_id,
            service_slug=service_slug,
            session_id=session_id,
            count=deleted,
        )
        return deleted
    except Exception as exc:
        logger.warning(
            "connector_code.r2_session_delete_failed",
            tenant_id=tenant_id,
            service_slug=service_slug,
            session_id=session_id,
            error=str(exc),
        )
        return 0


async def delete_connector_docs(tenant_id: str, provider: str, service_slug: str) -> bool:
    """Delete connector docs JSON from R2 (or local). Returns True if deleted, False if not found."""
    key = _docs_key(tenant_id, provider, service_slug)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            deleted = await loop.run_in_executor(None, partial(_local_delete, key))
        else:
            client = _get_client()
            await loop.run_in_executor(
                None,
                partial(client.delete_object, Bucket=_get_bucket(), Key=key),
            )
            deleted = True
        logger.info(
            "connector_docs.deleted",
            tenant_id=tenant_id,
            provider=provider,
            service_slug=service_slug,
        )
        return deleted
    except Exception as exc:
        logger.warning("connector_docs.delete_failed", key=key, error=str(exc))
        return False


# ── Connector code storage (R2 backend) ───────────────────────────────────────
#
# Key layout (tenant is already in the bucket name — no tenant prefix in key):
#   {coll}/connectors/{tenant_id}/{service_slug}/sessions/{session_id}/{rel_path}  ← draft
#
# Deployment to production is handled via GitHub CI/CD — not via R2 promotion.
# When R2 is not configured the same paths are mirrored under _LOCAL_CACHE_DIR so
# the local-dev and production code paths are identical.

_SKIP_DIRS_UPLOAD = {
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
    "dist",
    "build",
}


def _connector_draft_key(tenant_id: str, service_slug: str, session_id: str, rel_path: str) -> str:
    # Per-app buckets (shielva-agentic-app-*) are already installation-scoped —
    # no collection prefix and no tenant_id segment needed in the key.
    # Shared/legacy buckets keep the tenant_id for multi-tenant namespacing.
    if _get_bucket().startswith("shielva-agentic-app-"):
        return _k("connectors", service_slug, "sessions", session_id, rel_path)
    return _k(_coll(), "connectors", tenant_id, service_slug, "sessions", session_id, rel_path)


def connector_session_r2_prefix(tenant_id: str, service_slug: str, session_id: str) -> str:
    """Return the R2 key prefix (no trailing slash) for a session's connector code."""
    if _get_bucket().startswith("shielva-agentic-app-"):
        return _k("connectors", service_slug, "sessions", session_id)
    return _k(_coll(), "connectors", tenant_id, service_slug, "sessions", session_id)


def _content_type_for(rel_path: str) -> str:
    ext = Path(rel_path).suffix.lower()
    return {
        ".py": "text/x-python",
        ".json": "application/json",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".toml": "text/plain",
        ".yaml": "text/plain",
        ".yml": "text/plain",
        ".ini": "text/plain",
        ".cfg": "text/plain",
        ".sh": "text/x-sh",
    }.get(ext, "text/plain")


async def upload_connector_dir(tenant_id: str, service_slug: str, session_id: str, out_dir: "Path") -> int:
    """Upload every file in *out_dir* to R2 (or local cache).

    Skips __pycache__, .pyc files, and other build artifacts.
    Returns the number of files uploaded.
    """
    out_dir = Path(out_dir)
    if not out_dir.exists():
        logger.warning("connector_code.upload_dir_missing", path=str(out_dir))
        return 0

    files_to_upload: list[tuple[str, str]] = []  # (rel_path, content)
    for f in sorted(out_dir.rglob("*")):
        if not f.is_file():
            continue
        if any(part in _SKIP_DIRS_UPLOAD for part in f.parts):
            continue
        if f.suffix == ".pyc":
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue  # skip binary / unreadable files
        rel = str(f.relative_to(out_dir))
        files_to_upload.append((rel, content))

    if not files_to_upload:
        return 0

    loop = asyncio.get_event_loop()

    if _use_local():
        for rel, content in files_to_upload:
            key = _connector_draft_key(tenant_id, service_slug, session_id, rel)
            await loop.run_in_executor(None, partial(_local_write, key, content))
        logger.info(
            "connector_code.local_upload_done",
            tenant_id=tenant_id,
            service_slug=service_slug,
            session_id=session_id,
            count=len(files_to_upload),
        )
        return len(files_to_upload)

    # R2: upload files concurrently in batches of 10
    client = _get_client()
    bucket = _get_bucket()

    async def _upload_one(rel: str, content: str) -> None:
        key = _connector_draft_key(tenant_id, service_slug, session_id, rel)
        ct = _content_type_for(rel)
        await loop.run_in_executor(None, partial(_sync_write, client, bucket, key, content, ct))

    # Chunk into batches to avoid overwhelming the event loop
    BATCH = 10
    uploaded = 0
    for i in range(0, len(files_to_upload), BATCH):
        batch = files_to_upload[i : i + BATCH]
        await asyncio.gather(*[_upload_one(rel, content) for rel, content in batch])
        uploaded += len(batch)

    logger.info(
        "connector_code.r2_upload_done",
        tenant_id=tenant_id,
        service_slug=service_slug,
        session_id=session_id,
        count=uploaded,
    )
    return uploaded


async def list_connector_files(tenant_id: str, service_slug: str, session_id: str) -> list[str]:
    """Return sorted list of relative file paths stored in R2 for this session.

    Returns empty list if no files found or R2 not configured.
    """
    prefix = connector_session_r2_prefix(tenant_id, service_slug, session_id) + "/"
    loop = asyncio.get_event_loop()

    if _use_local():
        local_root = _local_path(connector_session_r2_prefix(tenant_id, service_slug, session_id))
        if not local_root.exists():
            return []
        return sorted(str(f.relative_to(local_root)) for f in local_root.rglob("*") if f.is_file())

    try:
        client = _get_client()
        bucket = _get_bucket()

        def _list_objects() -> list[str]:
            keys: list[str] = []
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    # Strip the prefix to get the relative path
                    keys.append(obj["Key"][len(prefix) :])
            return sorted(keys)

        return await loop.run_in_executor(None, _list_objects)
    except Exception as exc:
        logger.warning(
            "connector_code.list_failed",
            tenant_id=tenant_id,
            session_id=session_id,
            error=str(exc),
        )
        return []


async def get_connector_file(tenant_id: str, service_slug: str, session_id: str, rel_path: str) -> str | None:
    """Fetch a single connector file from R2 (or local cache). Returns None if not found."""
    key = _connector_draft_key(tenant_id, service_slug, session_id, rel_path)
    loop = asyncio.get_event_loop()

    if _use_local():
        return await loop.run_in_executor(None, partial(_local_read, key))

    try:
        client = _get_client()
        return await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
    except Exception as exc:
        logger.warning("connector_code.get_failed", key=key, error=str(exc))
        return None


async def get_connector_r2_checksums(tenant_id: str, service_slug: str, session_id: str) -> dict:
    """Return {rel_path: md5_hex} for all files currently stored in R2 (or local cache)
    for this connector session.  For real R2, the ETag is the MD5 (no quotes).
    """
    import hashlib as _hashlib

    prefix = connector_session_r2_prefix(tenant_id, service_slug, session_id) + "/"
    loop = asyncio.get_event_loop()

    if _use_local():
        local_root = _local_path(connector_session_r2_prefix(tenant_id, service_slug, session_id))
        if not local_root.exists():
            return {}
        result: dict = {}
        for f in local_root.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(local_root))
                try:
                    content = f.read_text(encoding="utf-8")
                    result[rel] = _hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()
                except Exception:
                    pass
        return result

    try:
        client = _get_client()
        bucket = _get_bucket()

        def _list_with_etags() -> dict:
            r: dict = {}
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    rel = obj["Key"][len(prefix) :]
                    if rel:
                        # ETag comes wrapped in quotes — strip them
                        etag = obj.get("ETag", "").strip('"').strip("'").lower()
                        r[rel] = etag
            return r

        return await loop.run_in_executor(None, _list_with_etags)
    except Exception as exc:
        logger.warning(
            "connector_code.r2_checksums_failed",
            tenant_id=tenant_id,
            session_id=session_id,
            error=str(exc),
        )
        return {}


async def upload_connector_files_selective(
    tenant_id: str, service_slug: str, session_id: str, out_dir: "Path", files: list
) -> int:
    """Upload only the specified relative-path files from out_dir to R2 (or local cache).
    Returns the number of files actually uploaded.
    """
    out_dir = Path(out_dir)
    if not out_dir.exists() or not files:
        return 0

    files_to_upload: list = []
    for rel in files:
        f = out_dir / rel
        if not f.is_file():
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        files_to_upload.append((rel, content))

    if not files_to_upload:
        return 0

    loop = asyncio.get_event_loop()

    if _use_local():
        for rel, content in files_to_upload:
            key = _connector_draft_key(tenant_id, service_slug, session_id, rel)
            await loop.run_in_executor(None, partial(_local_write, key, content))
        return len(files_to_upload)

    client = _get_client()
    bucket = _get_bucket()

    async def _upload_one(rel: str, content: str) -> None:
        key = _connector_draft_key(tenant_id, service_slug, session_id, rel)
        ct = _content_type_for(rel)
        await loop.run_in_executor(None, partial(_sync_write, client, bucket, key, content, ct))

    await asyncio.gather(*[_upload_one(rel, content) for rel, content in files_to_upload])
    logger.info(
        "connector_code.selective_upload_done",
        tenant_id=tenant_id,
        session_id=session_id,
        count=len(files_to_upload),
    )
    return len(files_to_upload)


async def delete_connector_r2_files(tenant_id: str, service_slug: str, session_id: str, rel_paths: list) -> int:
    """Delete specific files from R2 (or local cache) by relative path.
    Returns count deleted.
    """
    if not rel_paths:
        return 0

    loop = asyncio.get_event_loop()

    if _use_local():
        deleted = 0
        for rel in rel_paths:
            key = _connector_draft_key(tenant_id, service_slug, session_id, rel)
            if await loop.run_in_executor(None, partial(_local_delete, key)):
                deleted += 1
        return deleted

    client = _get_client()
    bucket = _get_bucket()

    # Build full R2 keys
    r2_keys = [_connector_draft_key(tenant_id, service_slug, session_id, rel) for rel in rel_paths]

    def _delete_batch(keys_batch: list) -> int:
        objects = [{"Key": k} for k in keys_batch]
        resp = client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        return len(resp.get("Deleted", []))

    # S3 delete_objects limit is 1000 per call
    deleted = 0
    BATCH = 1000
    for i in range(0, len(r2_keys), BATCH):
        batch = r2_keys[i : i + BATCH]
        deleted += await loop.run_in_executor(None, partial(_delete_batch, batch))

    logger.info(
        "connector_code.r2_delete_done",
        tenant_id=tenant_id,
        session_id=session_id,
        count=deleted,
    )
    return deleted


# ── Connector AI Analysis (docs research + top prompts) ───────────────────────
# Persisted at service level (shared across sessions for the same provider/service):
#   {coll}/{provider}/{service_slug}/connector_analysis.json


def _analysis_key(provider: str, service_slug: str) -> str:
    return _k(_coll(), provider, service_slug, "connector_analysis.json")


async def get_connector_analysis(provider: str, service_slug: str) -> dict[str, Any] | None:
    """Fetch cached AI analysis for a connector. Returns None if not yet generated."""
    key = _analysis_key(provider, service_slug)
    loop = asyncio.get_event_loop()

    if _use_local():
        raw = await loop.run_in_executor(None, partial(_local_read, key))
        return json.loads(raw) if raw else None

    try:
        client = _get_client()
        raw = await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
        if raw:
            return json.loads(raw)
        # R2 returned empty — fall through to local fallback
    except Exception as exc:
        logger.warning(
            "analysis.get_failed_r2",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )

    # Fall back to local filesystem (written there when R2 save failed or no R2 configured)
    try:
        local_raw = await loop.run_in_executor(None, partial(_local_read, key))
        if local_raw:
            logger.info(
                "analysis.loaded_from_local",
                provider=provider,
                service_slug=service_slug,
            )
            return json.loads(local_raw)
    except Exception as exc:
        logger.warning(
            "analysis.get_failed_local",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )

    return None


async def save_connector_analysis(provider: str, service_slug: str, analysis: dict[str, Any]) -> None:
    """Persist AI analysis for a connector to R2 (or local cache)."""
    key = _analysis_key(provider, service_slug)
    content = json.dumps(analysis, ensure_ascii=False, indent=2)
    loop = asyncio.get_event_loop()

    if _use_local():
        await loop.run_in_executor(None, partial(_local_write, key, content))
        return

    try:
        client = _get_client()
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, _get_bucket(), key, content, "application/json"),
        )
        logger.info("analysis.saved", provider=provider, service_slug=service_slug)
    except Exception as exc:
        logger.warning(
            "analysis.save_failed",
            provider=provider,
            service_slug=service_slug,
            error=str(exc),
        )
        # Fall back to local
        await loop.run_in_executor(None, partial(_local_write, key, content))


# ── Per-step execution output ─────────────────────────────────────────
# Key pattern: {coll}/{provider}/{service_slug}/{session_id}/step_outputs/{step_index}.json
#
# Stores the FULL stdout/stderr + per-step log buffer that the codegen
# service used to embed in `execution_results[].output` / `execution_results[].logs`
# inside the Mongo session document. Offloading these here keeps each Mongo
# session row tiny — Mongo holds only {step_index, status, duration_ms,
# started_at, finished_at}; the heavyweight bytes live in R2 and are pulled
# on demand by the Logs tab in the Builder.


def _step_output_key(provider: str, service_slug: str, session_id: str, step_index: int) -> str:
    return _k(
        _coll(),
        provider,
        service_slug,
        session_id,
        "step_outputs",
        f"{step_index}.json",
    )


async def save_step_output(
    provider: str,
    service_slug: str,
    session_id: str,
    step_index: int,
    payload: dict[str, Any],
) -> None:
    """Persist a step's stdout/stderr + logs to R2.

    Payload shape (matches what the codegen service used to put in the Mongo
    execution_results entry's ``output`` and ``logs`` fields):

        { "output": str, "logs": List[str | dict], "step_type": str, "command": str? }

    Gzip-compressed transparently when the encoded body is ≥ 1 KB (the
    default for any real step run). The reader (`get_step_output`) handles
    decompression automatically.
    """
    if not provider or not service_slug or not session_id:
        return
    key = _step_output_key(provider, service_slug, session_id, step_index)
    content = json.dumps(payload, ensure_ascii=False, default=str)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            await loop.run_in_executor(None, partial(_local_write, key, content))
        else:
            client = _get_client()
            await loop.run_in_executor(
                None,
                partial(_sync_write, client, _get_bucket(), key, content, "application/json"),
            )
        logger.info(
            "step_output.saved",
            provider=provider,
            service_slug=service_slug,
            session_id=session_id,
            step_index=step_index,
            bytes=len(content),
        )
    except Exception as exc:
        logger.warning("step_output.save_failed", key=key, error=str(exc))


async def get_step_output(
    provider: str,
    service_slug: str,
    session_id: str,
    step_index: int,
) -> dict[str, Any] | None:
    """Fetch a step's R2-stored output payload. Returns None when absent."""
    if not provider or not service_slug or not session_id:
        return None
    key = _step_output_key(provider, service_slug, session_id, step_index)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            raw = await loop.run_in_executor(None, partial(_local_read, key))
        else:
            client = _get_client()
            raw = await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("step_output.get_failed", key=key, error=str(exc))
    return None


# ── Per-session full plan (Phase 4) ───────────────────────────────────
# Key pattern: {coll}/{provider}/{service_slug}/{session_id}/plan_full.json
#
# Mongo keeps only a slim plan summary {version, steps:[{index,title,type,
# status}]} that the Manage Connectors list + Builder badges read on every
# page load. The FULL PlanDocument (per-step description, config, install
# fields, methods, etc.) lives here in R2 — written once on plan generation
# (or replan), read on demand when the Builder opens a session.


def _plan_full_key(provider: str, service_slug: str, session_id: str) -> str:
    return _k(_coll(), provider, service_slug, session_id, "plan_full.json")


async def save_plan_full(
    provider: str,
    service_slug: str,
    session_id: str,
    plan: dict[str, Any],
) -> None:
    """Persist the full PlanDocument (as a dict) to R2.

    Compressed transparently via ``_sync_write`` when the encoded body is
    ≥ 1 KB (always true for a real plan — each step description + config is
    multiple hundreds of bytes). The matching reader (``get_plan_full``)
    decompresses on the fly.
    """
    if not provider or not service_slug or not session_id or not plan:
        return
    key = _plan_full_key(provider, service_slug, session_id)
    content = json.dumps(plan, ensure_ascii=False, default=str)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            await loop.run_in_executor(None, partial(_local_write, key, content))
        else:
            client = _get_client()
            await loop.run_in_executor(
                None,
                partial(_sync_write, client, _get_bucket(), key, content, "application/json"),
            )
        logger.info(
            "plan_full.saved",
            provider=provider,
            service_slug=service_slug,
            session_id=session_id,
            bytes=len(content),
        )
    except Exception as exc:
        logger.warning("plan_full.save_failed", key=key, error=str(exc))


async def get_plan_full(
    provider: str,
    service_slug: str,
    session_id: str,
) -> dict[str, Any] | None:
    """Fetch the full PlanDocument dict from R2. Returns None when absent."""
    if not provider or not service_slug or not session_id:
        return None
    key = _plan_full_key(provider, service_slug, session_id)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            raw = await loop.run_in_executor(None, partial(_local_read, key))
        else:
            client = _get_client()
            raw = await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("plan_full.get_failed", key=key, error=str(exc))
    return None


# ── Conversation history (Phase 5) ─────────────────────────────────────
# Key pattern: {coll}/{provider}/{service_slug}/{session_id}/conversation_history.json
#
# Each Claude turn ({role, content}) appended to the planner's running
# conversation_history grows the array — replanning a complex connector
# can leave 20–50 turns × hundreds of tokens each. Embedding that in the
# Mongo session doc was the third-largest contributor after plan + docs.
#
# Mongo keeps a tiny pointer ({r2_offloaded:True, turn_count:N,
# last_turn_at:ts}); the array itself lives here, gzipped.


def _conversation_history_key(provider: str, service_slug: str, session_id: str) -> str:
    return _k(_coll(), provider, service_slug, session_id, "conversation_history.json")


async def save_conversation_history(
    provider: str,
    service_slug: str,
    session_id: str,
    history: list[dict[str, Any]],
) -> None:
    """Persist a session's full conversation history (list of {role,content}) to R2.

    The companion Mongo write should be a slim pointer dict — see
    planning_service.persist_conversation_history. Reader = get_conversation_history.
    """
    if not provider or not service_slug or not session_id:
        return
    key = _conversation_history_key(provider, service_slug, session_id)
    content = json.dumps(history or [], ensure_ascii=False, default=str)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            await loop.run_in_executor(None, partial(_local_write, key, content))
        else:
            client = _get_client()
            await loop.run_in_executor(
                None,
                partial(_sync_write, client, _get_bucket(), key, content, "application/json"),
            )
        logger.info(
            "conversation_history.saved",
            provider=provider,
            service_slug=service_slug,
            session_id=session_id,
            turns=len(history or []),
            bytes=len(content),
        )
    except Exception as exc:
        logger.warning("conversation_history.save_failed", key=key, error=str(exc))


async def get_conversation_history(
    provider: str,
    service_slug: str,
    session_id: str,
) -> list[dict[str, Any]] | None:
    """Fetch the conversation history list from R2. Returns None when absent."""
    if not provider or not service_slug or not session_id:
        return None
    key = _conversation_history_key(provider, service_slug, session_id)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            raw = await loop.run_in_executor(None, partial(_local_read, key))
        else:
            client = _get_client()
            raw = await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
    except Exception as exc:
        logger.warning("conversation_history.get_failed", key=key, error=str(exc))
    return None


# ── Sync-request heavy blobs (Phase 6) ─────────────────────────────────
# Key pattern: SYNC_REQUESTS/{tenant_id}/{sync_request_id}.json
#
# Each sync_request document used to embed the FULL PR file diff (`files[]`,
# 50–200 KB) and the security-audit JSON dump per CI gate
# (`ci_results[].details`, 10–50 KB each × ~6 gates). 658 requests × ~113 KB
# = ~74 MB pulled across the wire on every list call. Move the bytes to R2,
# keep only metadata in Mongo. Read paths fetch the R2 blob in one call when
# the detail panel opens.


def _sync_request_blob_key(tenant_id: str, sync_request_id: str) -> str:
    return _k("SYNC_REQUESTS", tenant_id, f"{sync_request_id}.json")


async def save_sync_request_blob(
    tenant_id: str,
    sync_request_id: str,
    payload: dict[str, Any],
) -> None:
    """Persist a sync_request's heavy fields (files + ci_results.details) to R2.

    Payload shape:
        { "files": [...], "ci_results_details": {gate_name: details_str, ...} }

    Gzip-compressed transparently by `_sync_write`. Failures are logged but
    not raised so the caller still gets a slim Mongo doc — degraded mode,
    not data loss.
    """
    if not tenant_id or not sync_request_id:
        return
    key = _sync_request_blob_key(tenant_id, sync_request_id)
    content = json.dumps(payload, ensure_ascii=False, default=str)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            await loop.run_in_executor(None, partial(_local_write, key, content))
        else:
            client = _get_client()
            await loop.run_in_executor(
                None,
                partial(_sync_write, client, _get_bucket(), key, content, "application/json"),
            )
        logger.info(
            "sync_request_blob.saved",
            tenant_id=tenant_id,
            sync_request_id=sync_request_id,
            bytes=len(content),
        )
    except Exception as exc:
        logger.warning("sync_request_blob.save_failed", key=key, error=str(exc))


async def get_sync_request_blob(
    tenant_id: str,
    sync_request_id: str,
) -> dict[str, Any] | None:
    """Fetch a sync_request's offloaded blob from R2. Returns None when absent."""
    if not tenant_id or not sync_request_id:
        return None
    key = _sync_request_blob_key(tenant_id, sync_request_id)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            raw = await loop.run_in_executor(None, partial(_local_read, key))
        else:
            client = _get_client()
            raw = await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("sync_request_blob.get_failed", key=key, error=str(exc))
    return None
