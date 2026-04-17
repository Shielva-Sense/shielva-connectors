"""Integration Builder — Failure Tracker.

Persists step failure reports to Cloudflare R2 and caches in Redis.
Falls back to local filesystem when R2 is not configured (mirrors r2_service pattern).

── R2 / local layout ──────────────────────────────────────────────────────
  {collection}/{provider}/{service}/failures/{failure_id}.md

── Redis keys ──────────────────────────────────────────────────────────────
  failure:active:{session_id}:{step_index}  → failure_id       (TTL: 48 h)
  failure:content:{failure_id}              → markdown content  (TTL: 48 h)

── failure_id format ───────────────────────────────────────────────────────
  {step_type}_{8-char-uuid}_failures
  e.g.  write_tests_a1b2c3d4_failures

── Lifecycle ───────────────────────────────────────────────────────────────
  1. Step FAILS   → create_failure(...)    → new failure_id, written to R2 + Redis
  2. Retry/Fix    → get_failure_context()  → Redis hit → R2 fallback → LLM context
  3. Per fix pass → append_fix_attempt()  → updates R2 doc + refreshes Redis
  4. Resolved     → resolve_failure()     → deletes R2 file, purges Redis keys
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from integration.core.config import settings

logger = structlog.get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_REDIS_TTL = 48 * 3600  # 48 hours
_ACTIVE_KEY = "failure:active:{session_id}:{step_index}"
_CONTENT_KEY = "failure:content:{failure_id}"

# Local fallback directory (mirrors r2_service._LOCAL_CACHE_DIR)
_LOCAL_CACHE_DIR = Path(settings.GENERATED_CODE_DIR).parent / "plan_cache"


# ── Helpers ─────────────────────────────────────────────────────────────────

def _failure_id(step_type: str, session_id: str) -> str:
    """Deterministic failure ID per step per session: {step_type}_{first-8-chars-of-session}_failures.

    Using the session prefix (not random) ensures there is exactly ONE failure file
    per step per session — repeated failures overwrite the same file instead of
    accumulating dozens of orphaned .md files in R2.
    """
    session_prefix = session_id[:8] if session_id else "unknown"
    return f"{step_type}_{session_prefix}_failures"


def _collection_prefix() -> str:
    return settings.R2_COLLECTION_PREFIX


def _r2_key(provider: str, service: str, tenant_id: str, failure_id: str) -> str:
    # tenant_id omitted from path — bucket is already tenant-scoped
    return f"{_collection_prefix()}/{provider}/{service}/failures/{failure_id}.md"


def _is_r2_configured() -> bool:
    return bool(
        settings.R2_ACCOUNT_ID
        and settings.R2_ACCESS_KEY_ID
        and settings.R2_SECRET_ACCESS_KEY
    )


def _get_r2_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


# ── R2 / local sync I/O ─────────────────────────────────────────────────────

def _sync_r2_read(key: str) -> Optional[str]:
    from botocore.exceptions import ClientError
    from integration.services.r2_service import _get_bucket
    try:
        client = _get_r2_client()
        resp = client.get_object(Bucket=_get_bucket(), Key=key)
        return resp["Body"].read().decode("utf-8")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NoSuchBucket"):
            return None
        logger.warning("failure_tracker.r2_read_error", key=key, error=str(exc))
        return None
    except Exception as exc:
        logger.warning("failure_tracker.r2_read_error", key=key, error=str(exc))
        return None


def _sync_r2_write(key: str, content: str) -> None:
    from integration.services.r2_service import _get_bucket
    client = _get_r2_client()
    client.put_object(
        Bucket=_get_bucket(),  # per-app bucket — failure data is session-specific
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/markdown",
    )


def _sync_r2_delete(key: str) -> None:
    from botocore.exceptions import ClientError
    from integration.services.r2_service import _get_bucket
    try:
        client = _get_r2_client()
        client.delete_object(Bucket=_get_bucket(), Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("NoSuchKey", "404"):
            logger.warning("failure_tracker.r2_delete_error", key=key, error=str(exc))
    except Exception as exc:
        logger.warning("failure_tracker.r2_delete_error", key=key, error=str(exc))


def _local_path(key: str) -> Path:
    return _LOCAL_CACHE_DIR / key


def _local_read(key: str) -> Optional[str]:
    p = _local_path(key)
    return p.read_text(encoding="utf-8") if p.exists() else None


def _local_write(key: str, content: str) -> None:
    p = _local_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _local_delete(key: str) -> None:
    p = _local_path(key)
    if p.exists():
        p.unlink(missing_ok=True)


# ── Redis helpers ────────────────────────────────────────────────────────────

async def _redis():
    """Return a connected Redis client (lazy, per-call)."""
    import redis.asyncio as aioredis
    client = aioredis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
    return client


async def _redis_get(key: str) -> Optional[str]:
    try:
        r = await _redis()
        val = await r.get(key)
        await r.aclose()
        return val
    except Exception as exc:
        logger.warning("failure_tracker.redis_get_error", key=key, error=str(exc))
        return None


async def _redis_set(key: str, value: str, ttl: int = _REDIS_TTL) -> None:
    try:
        r = await _redis()
        await r.set(key, value, ex=ttl)
        await r.aclose()
    except Exception as exc:
        logger.warning("failure_tracker.redis_set_error", key=key, error=str(exc))


async def _redis_delete(*keys: str) -> None:
    if not keys:
        return
    try:
        r = await _redis()
        await r.delete(*keys)
        await r.aclose()
    except Exception as exc:
        logger.warning("failure_tracker.redis_delete_error", keys=keys, error=str(exc))


# ── Markdown builder ─────────────────────────────────────────────────────────

def _build_failure_md(
    failure_id: str,
    session_id: str,
    step_type: str,
    step_index: int,
    provider: str,
    service: str,
    tenant_id: str,
    error_summary: str,
    full_output: str,
    created_at: str,
) -> str:
    lines = [
        f"# Failure Report: `{failure_id}`",
        "",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| **Session** | `{session_id}` |",
        f"| **Step** | `{step_type}` (index: {step_index}) |",
        f"| **Provider** | `{provider}` / `{service}` |",
        f"| **Tenant** | `{tenant_id}` |",
        f"| **Created** | {created_at} |",
        f"| **Status** | 🔴 open |",
        "",
        "## Error Summary",
        "",
        error_summary or "_No summary available._",
        "",
        "## Full Error Output",
        "",
        "```",
        (full_output or "").strip()[-6000:],  # cap at 6 KB
        "```",
        "",
        "## Fix Attempts",
        "",
        "_No fix attempts yet._",
        "",
    ]
    return "\n".join(lines)


def _append_attempt_to_md(
    content: str,
    attempt_number: int,
    outcome: str,          # "succeeded" | "failed"
    strategy: str,
    details: str,
    timestamp: str,
) -> str:
    """Append a fix-attempt block and update the Status field in the header table."""
    outcome_icon = "✅" if outcome == "succeeded" else "❌"
    status_icon = "🟢 resolved" if outcome == "succeeded" else "🔴 open"

    # Update Status row in metadata table
    content = content.replace("| **Status** | 🔴 open |", f"| **Status** | {status_icon} |")
    content = content.replace("| **Status** | 🟢 resolved |", f"| **Status** | {status_icon} |")

    # Replace placeholder on first attempt
    if "_No fix attempts yet._" in content:
        content = content.replace("_No fix attempts yet._", "")

    attempt_block = "\n".join([
        f"### Attempt {attempt_number} — {timestamp}",
        "",
        f"**Outcome:** {outcome_icon} {outcome}",
        f"**Strategy:** {strategy}",
        "",
        "**Details:**",
        "```",
        details.strip()[-3000:] if details else "—",
        "```",
        "",
    ])

    content = content.rstrip() + "\n\n" + attempt_block
    return content


# ── Storage layer (async, uses asyncio.to_thread for sync I/O) ───────────────

async def _storage_read(key: str) -> Optional[str]:
    if _is_r2_configured():
        return await asyncio.to_thread(_sync_r2_read, key)
    return await asyncio.to_thread(_local_read, key)


async def _storage_write(key: str, content: str) -> None:
    if _is_r2_configured():
        await asyncio.to_thread(_sync_r2_write, key, content)
    else:
        await asyncio.to_thread(_local_write, key, content)


async def _storage_delete(key: str) -> None:
    if _is_r2_configured():
        await asyncio.to_thread(_sync_r2_delete, key)
    else:
        await asyncio.to_thread(_local_delete, key)


# ── Public API ────────────────────────────────────────────────────────────────

async def create_failure(
    *,
    session_id: str,
    step_index: int,
    step_type: str,
    provider: str,
    service: str,
    tenant_id: str,
    error_summary: str,
    full_output: str,
) -> str:
    """Record a step failure in R2 + Redis — one file per step per session (overwrites).

    Uses a deterministic failure_id so repeated failures for the same step always
    overwrite the same file instead of accumulating orphaned .md files.

    Returns the failure_id.
    """
    fid = _failure_id(step_type, session_id)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    md = _build_failure_md(
        failure_id=fid,
        session_id=session_id,
        step_type=step_type,
        step_index=step_index,
        provider=provider,
        service=service,
        tenant_id=tenant_id,
        error_summary=error_summary,
        full_output=full_output,
        created_at=now,
    )

    r2_key = _r2_key(provider, service, tenant_id, fid)
    active_key = _ACTIVE_KEY.format(session_id=session_id, step_index=step_index)
    content_key = _CONTENT_KEY.format(failure_id=fid)

    # Delete any stale old failure file that used a different (random) ID
    old_fid = await _redis_get(active_key)
    if old_fid and old_fid != fid:
        old_r2_key = _r2_key(provider, service, tenant_id, old_fid)
        old_content_key = _CONTENT_KEY.format(failure_id=old_fid)
        await _storage_delete(old_r2_key)
        await _redis_delete(old_content_key)
        logger.info("failure_tracker.old_file_deleted", old_fid=old_fid, new_fid=fid)

    # Overwrite with fresh failure content
    try:
        await _storage_write(r2_key, md)
    except Exception as exc:
        logger.error("failure_tracker.create_write_error", failure_id=fid, error=str(exc))

    # Cache in Redis
    await _redis_set(active_key, fid)
    await _redis_set(content_key, md)

    logger.info(
        "failure_tracker.created",
        failure_id=fid,
        session_id=session_id,
        step_index=step_index,
        step_type=step_type,
        r2_key=r2_key,
    )
    return fid


async def get_failure_context(
    *,
    session_id: str,
    step_index: int,
    provider: str,
    service: str,
    tenant_id: str,
) -> Optional[Dict[str, Any]]:
    """Load the active failure context for a step.

    Cache hierarchy: Redis → R2/local → None.

    Returns a dict with keys:
      failure_id  — the ID string
      content     — the full markdown
      summary     — first ~500 chars of Error Summary section (for LLM context)
    or None if no failure exists for this step.
    """
    active_key = _ACTIVE_KEY.format(session_id=session_id, step_index=step_index)

    # 1. Redis — get the current failure_id
    fid = await _redis_get(active_key)

    if not fid:
        # No Redis entry — nothing to do (failure may have been resolved, or never created)
        return None

    content_key = _CONTENT_KEY.format(failure_id=fid)

    # 2. Redis content cache hit
    content = await _redis_get(content_key)

    if not content:
        # 3. R2 / local fallback
        r2_key = _r2_key(provider, service, tenant_id, fid)
        content = await _storage_read(r2_key)
        if content:
            # Re-populate Redis cache
            await _redis_set(content_key, content)
        else:
            logger.warning("failure_tracker.content_not_found", failure_id=fid, r2_key=r2_key)
            return None

    # Extract a short summary for LLM injection
    summary = _extract_summary(content)

    logger.info(
        "failure_tracker.context_loaded",
        failure_id=fid,
        session_id=session_id,
        step_index=step_index,
        cache_hit=bool(content),
    )
    return {"failure_id": fid, "content": content, "summary": summary}


async def append_fix_attempt(
    *,
    failure_id: str,
    provider: str,
    service: str,
    tenant_id: str,
    outcome: str,      # "succeeded" | "failed"
    strategy: str,
    details: str,
) -> None:
    """Append a fix-attempt section to the failure markdown and refresh cache."""
    r2_key = _r2_key(provider, service, tenant_id, failure_id)
    content_key = _CONTENT_KEY.format(failure_id=failure_id)

    # Load current content (cache first)
    content = await _redis_get(content_key)
    if not content:
        content = await _storage_read(r2_key)
    if not content:
        logger.warning("failure_tracker.append_no_content", failure_id=failure_id)
        return

    # Count existing attempts
    attempt_number = content.count("### Attempt ") + 1
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    updated = _append_attempt_to_md(
        content,
        attempt_number=attempt_number,
        outcome=outcome,
        strategy=strategy,
        details=details,
        timestamp=now,
    )

    # Persist
    try:
        await _storage_write(r2_key, updated)
    except Exception as exc:
        logger.error("failure_tracker.append_write_error", failure_id=failure_id, error=str(exc))

    # Refresh cache
    await _redis_set(content_key, updated)

    logger.info(
        "failure_tracker.attempt_appended",
        failure_id=failure_id,
        attempt_number=attempt_number,
        outcome=outcome,
    )


async def resolve_failure(
    *,
    session_id: str,
    step_index: int,
    provider: str,
    service: str,
    tenant_id: str,
) -> None:
    """Mark a failure as resolved: remove from R2 and purge Redis keys.

    Safe to call even when no failure exists (no-op).
    """
    active_key = _ACTIVE_KEY.format(session_id=session_id, step_index=step_index)

    fid = await _redis_get(active_key)
    if not fid:
        # Maybe Redis expired — scan R2 directly? Too expensive.
        # For now silently skip; the file will be orphaned but won't affect correctness.
        return

    content_key = _CONTENT_KEY.format(failure_id=fid)
    r2_key = _r2_key(provider, service, tenant_id, fid)

    # Mark as resolved in the doc before deleting (nice for audit / if delete fails)
    try:
        content = await _redis_get(content_key) or await _storage_read(r2_key)
        if content:
            resolved_content = content.replace("| **Status** | 🔴 open |", "| **Status** | 🟢 resolved |")
            resolved_content = resolved_content.replace("| **Status** | 🟡 in-progress |", "| **Status** | 🟢 resolved |")
            await _storage_write(r2_key, resolved_content)
    except Exception:
        pass

    # Delete from R2 / local
    try:
        await _storage_delete(r2_key)
    except Exception as exc:
        logger.warning("failure_tracker.resolve_delete_error", failure_id=fid, error=str(exc))

    # Purge Redis
    await _redis_delete(active_key, content_key)

    logger.info(
        "failure_tracker.resolved",
        failure_id=fid,
        session_id=session_id,
        step_index=step_index,
    )


# ── Internal helpers ─────────────────────────────────────────────────────────

def _extract_summary(content: str) -> str:
    """Pull the Error Summary section text (first ~600 chars) from a failure markdown."""
    try:
        marker = "## Error Summary"
        end_marker = "## Full Error Output"
        start = content.find(marker)
        if start == -1:
            return content[:500]
        start += len(marker)
        end = content.find(end_marker, start)
        snippet = content[start:end].strip() if end != -1 else content[start:start + 600].strip()
        return snippet[:600]
    except Exception:
        return content[:500]


def build_failure_context_for_llm(context: Optional[Dict[str, Any]]) -> str:
    """Format failure context into a compact string for injection into LLM prompts.

    Returns an empty string when context is None.
    """
    if not context:
        return ""

    fid = context.get("failure_id", "unknown")
    summary = context.get("summary", "")

    # Extract fix attempts section from full content
    full = context.get("content", "")
    fix_section = ""
    if "## Fix Attempts" in full:
        idx = full.find("## Fix Attempts")
        fix_section = full[idx:].strip()

    lines = [
        "── Previous Failure History ─────────────────────────────────────────────",
        f"Failure ID: {fid}",
        "",
        "Error Summary:",
        summary,
    ]
    if fix_section and "No fix attempts yet" not in fix_section:
        lines += ["", "Previous Fix Attempts (do NOT repeat these strategies):", fix_section]
    lines += ["─────────────────────────────────────────────────────────────────────"]
    return "\n".join(lines)
