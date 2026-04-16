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
import csv
import io
import json
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from integration.core.config import settings

# ── Per-request tenant bucket (set by middleware from X-Tenant-Name header) ──
# At request time this always has the correct bucket (tenant_name.lower()).
# Outside a request context (startup sync, background tasks) it falls back to
# _get_bucket().
_tenant_bucket_ctx: ContextVar[str] = ContextVar("tenant_bucket", default="")


def _get_bucket() -> str:
    """Return the R2 bucket name for the current context.

    Priority:
      1. settings.R2_BUCKET_NAME (explicit .env config — always correct bucket name)
      2. ContextVar set by TenantBucketMiddleware (X-Tenant-Name header, lowercased)
         — used only in multi-bucket deployments where R2_BUCKET_NAME is intentionally blank.

    Reason for this order: the tenant ID sent in X-Tenant-Name (e.g. "shielva-sense")
    often differs from the actual R2 bucket name (e.g. "shielvasense"). The configured
    R2_BUCKET_NAME is always the canonical bucket and must take precedence.
    """
    return settings.R2_BUCKET_NAME or _tenant_bucket_ctx.get()

logger = structlog.get_logger(__name__)

# ── Local cache directory (used when R2 is not configured) ───────────
# Lives INSIDE GENERATED_CODE_DIR (not its parent) so that all user data stays
# in the user's chosen project directory, never inside the shielva-connectors repo.
# Layout: {GENERATED_CODE_DIR}/plan_cache/{collection_prefix}/{provider}/{slug}/
_LOCAL_CACHE_DIR = Path(settings.GENERATED_CODE_DIR).resolve() / "plan_cache"

# ── Step type display labels ──────────────────────────────────────────

_STEP_TYPE_ICONS = {
    "install_deps":    "📦",
    "configure_auth":  "🔑",
    "scaffold_code":   "🏗",
    "write_connector": "⚙",
    "write_tests":     "🧪",
    "run_tests":       "▶",
}

_STEP_TYPE_LABELS = {
    "install_deps":    "Install Dependencies",
    "configure_auth":  "Configure Authentication",
    "scaffold_code":   "Scaffold Code Structure",
    "write_connector": "Write Connector",
    "write_tests":     "Write Tests",
    "run_tests":       "Run Tests",
}


# ── Markdown generator ────────────────────────────────────────────────

def _build_plan_markdown(
    provider: str,
    service: str,
    prompt: str,
    plan_data: Dict[str, Any],
    generated_at: str,
) -> str:
    """Convert a plan_data dict into a full human-readable markdown document."""
    steps: List[Dict[str, Any]] = plan_data.get("steps", [])
    version: int = plan_data.get("version", 1)
    package_structure: Optional[Dict[str, Any]] = plan_data.get("package_structure")
    recommended_features: List[Dict[str, Any]] = plan_data.get("recommended_features", [])

    lines: List[str] = []

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
        pkg_files: List[Dict[str, Any]] = package_structure.get("files", [])
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
        by_cat: Dict[str, List[Dict]] = {}
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
    """Return True if all R2 credentials are present in config."""
    return bool(
        settings.R2_ACCOUNT_ID
        and settings.R2_ACCESS_KEY_ID
        and settings.R2_SECRET_ACCESS_KEY
        and _get_bucket()
    )


def _use_local() -> bool:
    """True when R2 is not configured — use local filesystem fallback."""
    return not is_configured()


# ── Local filesystem helpers ─────────────────────────────────────────

def _local_path(key: str) -> Path:
    """Convert an R2-style key to a local filesystem path."""
    return _LOCAL_CACHE_DIR / key


def _local_read(key: str) -> Optional[str]:
    """Read a file from local cache. Returns None if not found."""
    path = _local_path(key)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _local_write(key: str, content: str) -> None:
    """Write a file to local cache, creating parent directories as needed."""
    path = _local_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _local_delete(key: str) -> bool:
    """Delete a file from local cache. Returns True if deleted, False if not found."""
    path = _local_path(key)
    if path.exists():
        path.unlink()
        return True
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
    """Return the collection key prefix (no trailing slash)."""
    return settings.R2_COLLECTION_PREFIX


def _csv_key(provider: str, service_slug: str, tenant_id: str = "") -> str:
    # tenant_id removed from path — the R2 bucket itself is already tenant-scoped
    return f"{_coll()}/{provider}/{service_slug}/prompts.csv"


def _plan_json_key(provider: str, service_slug: str, tenant_id: str = "") -> str:
    return f"{_coll()}/{provider}/{service_slug}/plan.json"


def _plan_md_key(provider: str, service_slug: str, tenant_id: str = "") -> str:
    return f"{_coll()}/{provider}/{service_slug}/plan.md"


def _progress_key(provider: str, service_slug: str, tenant_id: str = "") -> str:
    return f"{_coll()}/{provider}/{service_slug}/progress.json"


def _sync_read(client, bucket: str, key: str) -> Optional[str]:
    from botocore.exceptions import ClientError

    try:
        resp = client.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read().decode("utf-8")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NoSuchBucket"):
            return None
        raise


def _sync_write(
    client, bucket: str, key: str, content: str, content_type: str = "text/plain"
) -> None:
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType=content_type,
    )


def ensure_bucket() -> None:
    """Verify the tenant-root R2 bucket is accessible. Called at service startup.

    The bucket (shielvasense) is shared across all services and must already exist.
    Integration plans live at key prefix: {R2_COLLECTION_PREFIX}/
    """
    if _use_local():
        # Ensure local cache root exists
        _LOCAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("cache.local_dir_ready", path=str(_LOCAL_CACHE_DIR))
        return

    from botocore.exceptions import ClientError

    client = _get_client()
    bucket = _get_bucket()
    coll = settings.R2_COLLECTION_PREFIX
    try:
        client.head_bucket(Bucket=bucket)
        logger.info("r2.bucket_accessible", bucket=bucket, collection=coll)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        logger.warning("r2.bucket_check_failed", bucket=bucket, collection=coll, error_code=code)


# ── Public async API ──────────────────────────────────────────────────

async def get_history(
    provider: str, service_slug: str, tenant_id: str
) -> Optional[Dict[str, Any]]:
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
            None, partial(_sync_read, client, bucket, _progress_key(provider, service_slug, tenant_id))
        )

        if not progress_raw:
            logger.info("r2.no_progress", provider=provider, service_slug=service_slug, tenant_id=tenant_id)
            return None

        progress = json.loads(progress_raw)

        if not progress.get("plan_generated", False):
            logger.info("r2.plan_not_generated", provider=provider, service_slug=service_slug, tenant_id=tenant_id)
            return None

        # ── 2. plan_generated=true — fetch plan.json, plan.md, prompts.csv in parallel ──
        plan_raw, md_raw, csv_raw = await asyncio.gather(
            loop.run_in_executor(None, partial(_sync_read, client, bucket, _plan_json_key(provider, service_slug, tenant_id))),
            loop.run_in_executor(None, partial(_sync_read, client, bucket, _plan_md_key(provider, service_slug, tenant_id))),
            loop.run_in_executor(None, partial(_sync_read, client, bucket, _csv_key(provider, service_slug, tenant_id))),
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


def _local_get_history(
    provider: str, service_slug: str, tenant_id: str
) -> Optional[Dict[str, Any]]:
    """Local filesystem version of get_history."""
    try:
        progress_raw = _local_read(_progress_key(provider, service_slug, tenant_id))
        if not progress_raw:
            logger.info("cache.local_no_progress", provider=provider, service_slug=service_slug, tenant_id=tenant_id)
            return None

        progress = json.loads(progress_raw)
        if not progress.get("plan_generated", False):
            logger.info("cache.local_plan_not_generated", provider=provider, service_slug=service_slug)
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
        logger.warning("cache.local_get_history_failed", provider=provider, service_slug=service_slug, error=str(exc))
        return None


async def get_plan_markdown(provider: str, service_slug: str, tenant_id: str) -> Optional[str]:
    """Fetch only the plan.md. Works with R2 or local filesystem."""
    if _use_local():
        return _local_read(_plan_md_key(provider, service_slug, tenant_id))

    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()

    try:
        return await loop.run_in_executor(
            None, partial(_sync_read, client, bucket, _plan_md_key(provider, service_slug, tenant_id))
        )
    except Exception as exc:
        logger.warning("r2.get_md_failed", provider=provider, service_slug=service_slug, error=str(exc))
        return None


async def save_prompt_and_plan(
    provider: str,
    service_slug: str,
    tenant_id: str,
    prompt: str,
    plan_data: Dict[str, Any],
    guidelines_version: Optional[str] = None,
) -> None:
    """Save plan: overwrites prompts.csv, plan.json, plan.md, and progress.json.

    Uses R2 when configured, local filesystem otherwise.
    Ensures the bucket/directory exists before writing.
    Raises on write failure so the caller can emit accurate logs to the UI.
    """
    generated_at = datetime.now(timezone.utc).isoformat()

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
                partial(_sync_write, client, bucket, _csv_key(provider, service_slug, tenant_id), csv_content, "text/csv"),
            ),
            loop.run_in_executor(
                None,
                partial(_sync_write, client, bucket,
                        _plan_json_key(provider, service_slug, tenant_id), plan_json, "application/json"),
            ),
            loop.run_in_executor(
                None,
                partial(_sync_write, client, bucket,
                        _plan_md_key(provider, service_slug, tenant_id), md, "text/markdown"),
            ),
            loop.run_in_executor(
                None,
                partial(_sync_write, client, bucket,
                        _progress_key(provider, service_slug, tenant_id), progress_json, "application/json"),
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
    guidelines_version: Optional[str] = None,
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

async def get_cached_guidelines_version(
    provider: str, service_slug: str, tenant_id: str
) -> Optional[str]:
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
            None, partial(_sync_read, client, bucket, _progress_key(provider, service_slug, tenant_id))
        )
        if not raw:
            return None
        return json.loads(raw).get("guidelines_version")
    except Exception:
        return None


async def invalidate_stale_plan(
    provider: str, service_slug: str, tenant_id: str
) -> None:
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
        logger.info("cache.disk_plan_invalidated", provider=provider, service_slug=service_slug, tenant_id=tenant_id)
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
            None, partial(_sync_write, client, bucket, key, updated_json, "application/json")
        )
        logger.info("r2.plan_invalidated", provider=provider, service_slug=service_slug, tenant_id=tenant_id)
    except Exception as exc:
        logger.warning("r2.plan_invalidate_failed", error=str(exc))


# ── Execution state ───────────────────────────────────────────────────

def _execution_state_key(provider: str, service_slug: str, tenant_id: str = "") -> str:
    # tenant_id removed — bucket is already tenant-scoped
    return f"{_coll()}/{provider}/{service_slug}/execution_state.json"


async def get_execution_state(
    provider: str, service_slug: str, tenant_id: str
) -> Optional[Dict[str, Any]]:
    """Read execution_state.json. Works with R2 or local filesystem."""
    if _use_local():
        raw = _local_read(_execution_state_key(provider, service_slug, tenant_id))
        return json.loads(raw) if raw else None

    loop = asyncio.get_event_loop()
    client = _get_client()
    try:
        raw = await loop.run_in_executor(
            None, partial(_sync_read, client, _get_bucket(),
                          _execution_state_key(provider, service_slug, tenant_id))
        )
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("r2.get_execution_state_failed", provider=provider, service_slug=service_slug, error=str(exc))
        return None


async def save_execution_state(
    provider: str,
    service_slug: str,
    tenant_id: str,
    completed_steps: List[str],
    session_id: str,
) -> None:
    """Write execution_state.json. Works with R2 or local filesystem."""
    state = {
        "tenant_id": tenant_id,
        "provider": provider,
        "service_slug": service_slug,
        "completed_steps": completed_steps,
        "last_session_id": session_id,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    state_json = json.dumps(state, indent=2)

    key = _execution_state_key(provider, service_slug, tenant_id)

    # Write to disk FIRST — source of truth
    try:
        _local_write(key, state_json)
        logger.info("cache.disk_execution_state_saved", provider=provider, service_slug=service_slug,
                    completed=completed_steps)
    except Exception as exc:
        logger.warning("cache.disk_save_execution_state_failed", provider=provider, service_slug=service_slug, error=str(exc))

    # Also write to R2 if configured
    if _use_local():
        return

    loop = asyncio.get_event_loop()
    client = _get_client()
    try:
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, _get_bucket(), key, state_json, "application/json")
        )
        logger.info("r2.execution_state_saved", provider=provider, service_slug=service_slug,
                    completed=completed_steps)
    except Exception as exc:
        logger.warning("r2.save_execution_state_failed", provider=provider, service_slug=service_slug, error=str(exc))


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
        raw = await loop.run_in_executor(
            None, partial(_sync_read, client, bucket, key)
        )
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
        progress["last_updated"] = datetime.now(timezone.utc).isoformat()
        updated_json = json.dumps(progress, indent=2)
        _local_write(key, updated_json)
        logger.info("cache.disk_stepper_max_step_updated", provider=provider, service_slug=service_slug, step_index=step_index)
    except Exception as exc:
        logger.warning("cache.disk_stepper_max_step_update_failed", error=str(exc))
        updated_json = json.dumps({"stepper_max_step": step_index, "last_updated": datetime.now(timezone.utc).isoformat()}, indent=2)

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
        logger.info("r2.stepper_max_step_updated", provider=provider, service_slug=service_slug, step_index=step_index)
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
        progress["last_updated"] = datetime.now(timezone.utc).isoformat()
        updated_json = json.dumps(progress, indent=2)
        _local_write(key, updated_json)
        logger.info("cache.disk_approval_updated", provider=provider, service_slug=service_slug, tenant_id=tenant_id, status=status)
    except Exception as exc:
        logger.warning("cache.disk_approval_update_failed", error=str(exc))
        updated_json = json.dumps({"approval_made": status, "last_updated": datetime.now(timezone.utc).isoformat()}, indent=2)

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
        logger.info("r2.approval_updated", provider=provider, service_slug=service_slug, tenant_id=tenant_id, status=status)
    except Exception as exc:
        logger.warning("r2.approval_update_failed", error=str(exc))


# ── Cache purge (called on session delete) ────────────────────────────

async def clear_cache(provider: str, service_slug: str, tenant_id: str) -> None:
    """Delete all cached plan/execution/failure files for a provider/service_slug/tenant.

    Called when a connector session is deleted so a fresh run starts from scratch.
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
        failures_dir = _local_path(f"{_coll()}/{provider}/{service_slug}/failures")
        if failures_dir.exists() and failures_dir.is_dir():
            import shutil
            try:
                shutil.rmtree(str(failures_dir))
                removed.append(f"{provider}/{service_slug}/failures/")
            except Exception as exc:
                logger.warning("cache.local_failures_clear_failed", error=str(exc))

        logger.info("cache.local_cleared", provider=provider, service_slug=service_slug,
                    tenant_id=tenant_id, removed=removed)
        return

    # R2 path
    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()

    async def _delete_key(key: str) -> None:
        try:
            await loop.run_in_executor(
                None, partial(client.delete_object, Bucket=bucket, Key=key)
            )
        except Exception as exc:
            logger.warning("r2.clear_key_failed", key=key, error=str(exc))

    # Delete all known flat files
    await asyncio.gather(*[_delete_key(k) for k in keys])

    # List and delete all failures/ objects under this prefix
    failures_prefix = f"{_coll()}/{provider}/{service_slug}/failures/"
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

    logger.info("r2.cache_cleared", provider=provider, service_slug=service_slug, tenant_id=tenant_id)


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
_step_prompt_cache: Dict[str, str] = {}


def _step_prompt_key(prompt_name: str) -> str:
    return f"{_coll()}/{_STEP_PROMPTS_PREFIX}/{prompt_name}.txt"


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
            bucket = _get_bucket()
            raw = await loop.run_in_executor(
                None, partial(_sync_read, client, bucket, key)
            )
            if raw:
                logger.debug("step_prompt.loaded_r2", prompt=prompt_name)
                return raw
        except Exception as exc:
            logger.warning("step_prompt.r2_read_failed", prompt=prompt_name, error=str(exc))

    # Seed local disk on first miss so future restarts read from disk
    if local_fallback and _use_local():
        local_path = _local_path(key)
        if not local_path.exists():
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(local_fallback, encoding="utf-8")
            logger.info("step_prompt.seeded_local", prompt=prompt_name, chars=len(local_fallback))

    logger.debug("step_prompt.using_local_fallback", prompt=prompt_name)
    return local_fallback


async def get_step_prompt(
    prompt_name: str,
    local_fallback: str,
    *,
    auth_type: Optional[str] = None,
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
        composed = (
            f"{base}\n\n"
            f"## Auth-Type Specific Rules — {auth_type}\n\n"
            f"{addendum}"
        )
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
        local_path = _local_path(key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(content, encoding="utf-8")
        logger.info("step_prompt.saved_local", prompt=prompt_name, chars=len(content))
        return

    loop = asyncio.get_event_loop()
    client = _get_client()
    bucket = _get_bucket()
    await loop.run_in_executor(
        None,
        partial(_sync_write, client, bucket, key, content, "text/plain"),
    )
    logger.info("step_prompt.saved_r2", prompt=prompt_name, chars=len(content))


async def sync_all_step_prompts_to_r2() -> Dict[str, str]:
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
        TEST_SYSTEM_PROMPT,
        FIX_CODE_PROMPT,
        FIX_TESTS_PROMPT,
        FIX_CONNECTOR_FOR_TESTS_PROMPT,
        TEST_RULES_GENERATION_PROMPT,
        MODULE_FILE_SYSTEM_PROMPT,
        TEST_MODULE_SYSTEM_PROMPT,
        USER_MODIFY_PROMPT,
        USER_RESTRUCTURE_PROMPT,
    )
    from integration.services.step_executor import _METADATA_SYSTEM_PROMPT, _SETUP_INSTRUCTIONS_SYSTEM, _TEST_GUIDELINES_SYSTEM
    from integration.services.agentic_fix import (
        _CONNECTOR_GEN_SYSTEM,
        _METADATA_GEN_SYSTEM,
        _DOCS_GEN_SYSTEM,
        _FIX_SYSTEM,
        _AUTH_TYPE_ADDENDA,
        _TEST_GEN_SYSTEM,
        _CONNECTOR_FIX_SYSTEM,
    )
    from integration.prompts.planning_prompt import PLANNING_SYSTEM_PROMPT, REPLAN_SYSTEM_PROMPT
    from integration.prompts.docs_prompt import DOCS_GENERATION_PROMPT, DOCS_UPDATE_PROMPT
    from integration.services.code_analysis_service import _ANALYSIS_SYSTEM
    from integration.services.docs_synth_service import _SYNTHESIS_PROMPT, _EXTRACTION_PROMPT

    prompts = {
        "CONNECTOR_SYSTEM_PROMPT": CONNECTOR_SYSTEM_PROMPT,
        "TEST_SYSTEM_PROMPT": TEST_SYSTEM_PROMPT,
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

    results: Dict[str, str] = {}

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
                existing = await loop.run_in_executor(
                    None, partial(_sync_read, client, _get_bucket(), key)
                )

            if existing and existing.strip() == content.strip():
                results[name] = "skipped"
                logger.debug("step_prompt.sync_skipped", prompt=name)
            else:
                # Upload if new OR if content has changed since last sync
                if existing:
                    logger.info("step_prompt.sync_updated", prompt=name,
                                reason="content changed — overwriting stale R2 version")
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
    return f"{_coll()}/{_DOCS_PREFIX}/{provider}/{service_slug}/docs.json"


async def get_connector_docs(
    tenant_id: str, provider: str, service_slug: str
) -> Optional[dict]:
    """Load connector docs JSON from R2 (or local). Returns None if not found."""
    key = _docs_key(tenant_id, provider, service_slug)
    loop = asyncio.get_event_loop()
    try:
        if _use_local():
            raw = await loop.run_in_executor(None, _local_read, key)
        else:
            client = _get_client()
            raw = await loop.run_in_executor(
                None, partial(_sync_read, client, _get_bucket(), key)
            )
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning("connector_docs.get_failed", key=key, error=str(exc))
    return None


async def save_connector_docs(
    tenant_id: str, provider: str, service_slug: str, docs: dict
) -> None:
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
                    _sync_write, client, _get_bucket(), key, content,
                    "application/json",
                ),
            )
        logger.info("connector_docs.saved", tenant_id=tenant_id, provider=provider, service_slug=service_slug)
    except Exception as exc:
        logger.warning("connector_docs.save_failed", key=key, error=str(exc))


# ── Test Guidelines storage ───────────────────────────────────────────
# Key pattern: {collection}/{provider}/{service_slug}/test_guidelines.md
# Stored as markdown. Falls back to local filesystem when R2 not configured.


def _implementation_plan_key(provider: str, service_slug: str) -> str:
    """R2 key: {collection}/{provider}/{service_slug}/implementation_plan.md"""
    return f"{_coll()}/{provider}/{service_slug}/implementation_plan.md"


async def get_implementation_plan(provider: str, service_slug: str) -> Optional[str]:
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
        content = await loop.run_in_executor(
            None, partial(_sync_read, client, _get_bucket(), key)
        )
        # Seed disk from R2 for future reads
        if content:
            try:
                _local_write(key, content)
            except Exception:
                pass
        return content
    except Exception as exc:
        logger.warning("implementation_plan.get_failed", provider=provider, service_slug=service_slug, error=str(exc))
        return None


async def store_implementation_plan(provider: str, service_slug: str, content: str) -> None:
    """Store connector-specific implementation plan — disk first, then R2."""
    key = _implementation_plan_key(provider, service_slug)
    loop = asyncio.get_event_loop()

    # Write to disk FIRST — source of truth
    await loop.run_in_executor(None, partial(_local_write, key, content))
    logger.info("implementation_plan.saved_disk", provider=provider, service_slug=service_slug, chars=len(content))

    # Also write to R2 if configured
    if _use_local():
        return
    try:
        client = _get_client()
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, _get_bucket(), key, content, "text/markdown"),
        )
        logger.info("implementation_plan.saved_r2", provider=provider, service_slug=service_slug, chars=len(content))
    except Exception as exc:
        logger.warning("implementation_plan.save_r2_failed", provider=provider, service_slug=service_slug, error=str(exc))
        raise


def _test_guidelines_key(provider: str, service_slug: str) -> str:
    """R2 key: {collection}/{provider}/{service_slug}/test_guidelines.md"""
    return f"{_coll()}/{provider}/{service_slug}/test_guidelines.md"


async def get_test_guidelines(provider: str, service_slug: str) -> Optional[str]:
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
        content = await loop.run_in_executor(
            None, partial(_sync_read, client, _get_bucket(), key)
        )
        # Seed disk from R2 for future reads
        if content:
            try:
                _local_write(key, content)
            except Exception:
                pass
        return content
    except Exception as exc:
        logger.warning("test_guidelines.get_failed", provider=provider, service_slug=service_slug, error=str(exc))
        return None


async def store_test_guidelines(provider: str, service_slug: str, content: str) -> None:
    """Store connector-specific test guidelines — disk first, then R2."""
    key = _test_guidelines_key(provider, service_slug)
    loop = asyncio.get_event_loop()

    # Write to disk FIRST — source of truth
    await loop.run_in_executor(None, partial(_local_write, key, content))
    logger.info("test_guidelines.saved_disk", provider=provider, service_slug=service_slug, chars=len(content))

    # Also write to R2 if configured
    if _use_local():
        return
    try:
        client = _get_client()
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, _get_bucket(), key, content, "text/markdown"),
        )
        logger.info("test_guidelines.saved_r2", provider=provider, service_slug=service_slug, chars=len(content))
    except Exception as exc:
        logger.warning("test_guidelines.save_r2_failed", provider=provider, service_slug=service_slug, error=str(exc))
        raise


def _setup_instructions_key(provider: str, service_slug: str) -> str:
    """R2 key: {collection}/{provider}/{service_slug}/setup_instructions.md"""
    return f"{_coll()}/{provider}/{service_slug}/setup_instructions.md"


async def get_setup_instructions(provider: str, service_slug: str) -> Optional[str]:
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
        content = await loop.run_in_executor(
            None, partial(_sync_read, client, _get_bucket(), key)
        )
        # Seed disk from R2 for future reads
        if content:
            try:
                _local_write(key, content)
            except Exception:
                pass
        return content
    except Exception as exc:
        logger.warning("setup_instructions.get_failed", provider=provider, service_slug=service_slug, error=str(exc))
        return None


async def store_setup_instructions(provider: str, service_slug: str, content: str) -> None:
    """Store connector-specific setup instructions — disk first, then R2."""
    key = _setup_instructions_key(provider, service_slug)
    loop = asyncio.get_event_loop()

    # Write to disk FIRST — source of truth
    try:
        await loop.run_in_executor(None, partial(_local_write, key, content))
        logger.info("setup_instructions.saved_disk", provider=provider, service_slug=service_slug, chars=len(content))
    except Exception as exc:
        logger.warning("setup_instructions.disk_save_failed", provider=provider, service_slug=service_slug, error=str(exc))

    # Also write to R2 if configured
    if _use_local():
        return
    try:
        client = _get_client()
        await loop.run_in_executor(
            None,
            partial(_sync_write, client, _get_bucket(), key, content, "text/markdown"),
        )
        logger.info("setup_instructions.saved_r2", provider=provider, service_slug=service_slug, chars=len(content))
    except Exception as exc:
        logger.warning("setup_instructions.save_r2_failed", provider=provider, service_slug=service_slug, error=str(exc))


def _entity_builder_key(provider: str, service_slug: str, method_name: str) -> str:
    """R2 key: {collection}/{provider}/{service_slug}/entity_builder_{method_name}.json"""
    return f"{_coll()}/{provider}/{service_slug}/entity_builder_{method_name}.json"


async def store_entity_builder_config(
    provider: str, service_slug: str, method_name: str, config: dict
) -> None:
    """Persist entity builder config (entity + mappings + method) to R2 for use in implement_persistence step."""
    import json as _json
    key = _entity_builder_key(provider, service_slug, method_name)
    content = _json.dumps(config, indent=2, default=str)
    loop = asyncio.get_event_loop()

    # ── Write to disk FIRST — source of truth ──
    try:
        await loop.run_in_executor(None, partial(_local_write, key, content))
        logger.info("entity_builder.saved_disk", provider=provider, service_slug=service_slug, method=method_name)
    except Exception as exc:
        logger.warning("entity_builder.disk_save_failed", error=str(exc))

    # ── Also write to R2 if configured ──
    if _use_local():
        return
    try:
        client = _get_client()
        await loop.run_in_executor(
            None, partial(_sync_write, client, _get_bucket(), key, content, "application/json")
        )
        logger.info("entity_builder.saved_r2", provider=provider, service_slug=service_slug, method=method_name)
    except Exception as exc:
        logger.warning("entity_builder.save_r2_failed", error=str(exc))
        raise


async def get_entity_builder_config(
    provider: str, service_slug: str, method_name: str
) -> Optional[dict]:
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
        logger.warning("entity_builder.get_failed", provider=provider, service_slug=service_slug, method=method_name, error=str(exc))
        return None


async def delete_connector_docs(
    tenant_id: str, provider: str, service_slug: str
) -> bool:
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
        logger.info("connector_docs.deleted", tenant_id=tenant_id, provider=provider, service_slug=service_slug)
        return deleted
    except Exception as exc:
        logger.warning("connector_docs.delete_failed", key=key, error=str(exc))
        return False


# ── Connector code storage (production-grade R2 backend) ──────────────────────
#
# Key layout (tenant is already in the bucket name — no tenant prefix in key):
#   {coll}/connectors/{tenant_id}/{service_slug}/sessions/{session_id}/{rel_path}  ← draft
#   {coll}/connectors/{tenant_id}/{service_slug}/production/{rel_path}             ← promoted
#
# When R2 is not configured the same paths are mirrored under _LOCAL_CACHE_DIR so
# the local-dev and production code paths are identical.

_SKIP_DIRS_UPLOAD = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", "node_modules", "dist", "build"}


def _connector_draft_key(tenant_id: str, service_slug: str, session_id: str, rel_path: str) -> str:
    return f"{_coll()}/connectors/{tenant_id}/{service_slug}/sessions/{session_id}/{rel_path}"


def _connector_prod_key(tenant_id: str, service_slug: str, rel_path: str) -> str:
    return f"{_coll()}/connectors/{tenant_id}/{service_slug}/production/{rel_path}"


def connector_session_r2_prefix(tenant_id: str, service_slug: str, session_id: str) -> str:
    """Return the R2 key prefix (no trailing slash) for a session's connector code."""
    return f"{_coll()}/connectors/{tenant_id}/{service_slug}/sessions/{session_id}"


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


async def upload_connector_dir(
    tenant_id: str, service_slug: str, session_id: str, out_dir: "Path"
) -> int:
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
            tenant_id=tenant_id, service_slug=service_slug,
            session_id=session_id, count=len(files_to_upload),
        )
        return len(files_to_upload)

    # R2: upload files concurrently in batches of 10
    client = _get_client()
    bucket = _get_bucket()

    async def _upload_one(rel: str, content: str) -> None:
        key = _connector_draft_key(tenant_id, service_slug, session_id, rel)
        ct = _content_type_for(rel)
        await loop.run_in_executor(
            None, partial(_sync_write, client, bucket, key, content, ct)
        )

    # Chunk into batches to avoid overwhelming the event loop
    BATCH = 10
    uploaded = 0
    for i in range(0, len(files_to_upload), BATCH):
        batch = files_to_upload[i : i + BATCH]
        await asyncio.gather(*[_upload_one(rel, content) for rel, content in batch])
        uploaded += len(batch)

    logger.info(
        "connector_code.r2_upload_done",
        tenant_id=tenant_id, service_slug=service_slug,
        session_id=session_id, count=uploaded,
    )
    return uploaded


async def list_connector_files(
    tenant_id: str, service_slug: str, session_id: str
) -> list[str]:
    """Return sorted list of relative file paths stored in R2 for this session.

    Returns empty list if no files found or R2 not configured.
    """
    prefix = connector_session_r2_prefix(tenant_id, service_slug, session_id) + "/"
    loop = asyncio.get_event_loop()

    if _use_local():
        local_root = _local_path(f"{_coll()}/connectors/{tenant_id}/{service_slug}/sessions/{session_id}")
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
                    keys.append(obj["Key"][len(prefix):])
            return sorted(keys)

        return await loop.run_in_executor(None, _list_objects)
    except Exception as exc:
        logger.warning("connector_code.list_failed", tenant_id=tenant_id, session_id=session_id, error=str(exc))
        return []


async def get_connector_file(
    tenant_id: str, service_slug: str, session_id: str, rel_path: str
) -> Optional[str]:
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


async def promote_connector(
    tenant_id: str, service_slug: str, session_id: str
) -> int:
    """Promote a connector session to production by copying session files → production/ prefix.

    This is the approval gate: callers should verify the session is complete before calling.
    Returns the number of files promoted.
    """
    file_list = await list_connector_files(tenant_id, service_slug, session_id)
    if not file_list:
        logger.warning(
            "connector_code.promote_empty",
            tenant_id=tenant_id, service_slug=service_slug, session_id=session_id,
        )
        return 0

    loop = asyncio.get_event_loop()

    if _use_local():
        for rel in file_list:
            src_key = _connector_draft_key(tenant_id, service_slug, session_id, rel)
            dst_key = _connector_prod_key(tenant_id, service_slug, rel)
            content = await loop.run_in_executor(None, partial(_local_read, src_key))
            if content is not None:
                await loop.run_in_executor(None, partial(_local_write, dst_key, content))
        logger.info(
            "connector_code.local_promote_done",
            tenant_id=tenant_id, service_slug=service_slug,
            session_id=session_id, count=len(file_list),
        )
        return len(file_list)

    client = _get_client()
    bucket = _get_bucket()

    async def _copy_one(rel: str) -> None:
        src_key = _connector_draft_key(tenant_id, service_slug, session_id, rel)
        dst_key = _connector_prod_key(tenant_id, service_slug, rel)
        # R2 supports same-bucket copy via copy_object
        await loop.run_in_executor(
            None,
            partial(
                client.copy_object,
                Bucket=bucket,
                CopySource={"Bucket": bucket, "Key": src_key},
                Key=dst_key,
            ),
        )

    BATCH = 10
    promoted = 0
    for i in range(0, len(file_list), BATCH):
        batch = file_list[i : i + BATCH]
        await asyncio.gather(*[_copy_one(rel) for rel in batch])
        promoted += len(batch)

    logger.info(
        "connector_code.r2_promote_done",
        tenant_id=tenant_id, service_slug=service_slug,
        session_id=session_id, count=promoted,
    )
    return promoted


async def get_production_file(
    tenant_id: str, service_slug: str, rel_path: str
) -> Optional[str]:
    """Fetch a file from the production/ prefix. Used by CMS to read deployed connectors."""
    key = _connector_prod_key(tenant_id, service_slug, rel_path)
    loop = asyncio.get_event_loop()

    if _use_local():
        return await loop.run_in_executor(None, partial(_local_read, key))

    try:
        client = _get_client()
        return await loop.run_in_executor(None, partial(_sync_read, client, _get_bucket(), key))
    except Exception as exc:
        logger.warning("connector_code.get_production_failed", key=key, error=str(exc))
        return None


# ── Connector AI Analysis (docs research + top prompts) ───────────────────────
# Persisted at service level (shared across sessions for the same provider/service):
#   {coll}/{provider}/{service_slug}/connector_analysis.json

def _analysis_key(provider: str, service_slug: str) -> str:
    return f"{_coll()}/{provider}/{service_slug}/connector_analysis.json"


async def get_connector_analysis(provider: str, service_slug: str) -> Optional[Dict[str, Any]]:
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
        logger.warning("analysis.get_failed_r2", provider=provider, service_slug=service_slug, error=str(exc))

    # Fall back to local filesystem (written there when R2 save failed or no R2 configured)
    try:
        local_raw = await loop.run_in_executor(None, partial(_local_read, key))
        if local_raw:
            logger.info("analysis.loaded_from_local", provider=provider, service_slug=service_slug)
            return json.loads(local_raw)
    except Exception as exc:
        logger.warning("analysis.get_failed_local", provider=provider, service_slug=service_slug, error=str(exc))

    return None


async def save_connector_analysis(provider: str, service_slug: str, analysis: Dict[str, Any]) -> None:
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
            None, partial(_sync_write, client, _get_bucket(), key, content, "application/json")
        )
        logger.info("analysis.saved", provider=provider, service_slug=service_slug)
    except Exception as exc:
        logger.warning("analysis.save_failed", provider=provider, service_slug=service_slug, error=str(exc))
        # Fall back to local
        await loop.run_in_executor(None, partial(_local_write, key, content))
