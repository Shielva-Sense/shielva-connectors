"""Sync Request routes — GitHub-backed connector sync with in-house CI pipeline.

Sync requests are tenant-scoped: every authenticated user in a tenant sees the
same queue. A hold mechanism prevents concurrent conflicting syncs.

Endpoints:
  POST   /sync-requests/raise          — raise a new sync request (triggers CI)
  GET    /sync-requests                 — list sync requests for tenant
  GET    /sync-requests/{id}            — get single sync request with diff + CI results
  POST   /sync-requests/{id}/approve    — approve & merge the PR
  POST   /sync-requests/{id}/hold       — hold the sync queue
  POST   /sync-requests/{id}/unhold     — release the hold
  POST   /sync-requests/{id}/dismiss    — dismiss without merging
  GET    /sync-requests/events          — SSE stream for real-time updates
  GET    /sync-settings                 — get tenant sync config
  PUT    /sync-settings                 — update tenant sync config
"""

import asyncio
import functools
import hashlib
import json
import os
import random
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import structlog
from bson import ObjectId
from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pymongo import ReturnDocument

from integration.core.config import settings
from integration.db.database import get_db
from integration.services import r2_service

logger = structlog.get_logger(__name__)

# Limit concurrent CI pipelines to avoid GitHub secondary rate limits when
# many sync requests are raised or retried at the same time.
_CI_SEMAPHORE = asyncio.Semaphore(5)

sync_request_router = APIRouter(prefix="/sync-requests", tags=["sync-requests"])

# ── Branch access by role ────────────────────────────────────────────────────

BRANCH_ACCESS: Dict[str, List[str]] = {
    # Platform identity roles (issued by the identity service in the JWT `role` claim)
    "platform_owner": ["connector-development", "qa", "uat", "master", "main"],
    "org_owner": ["connector-development", "qa", "uat"],
    # Sync-route operational roles
    "super_admin": ["connector-development", "qa", "uat", "master", "main"],
    "tenant_admin": ["connector-development", "qa", "uat"],
    "bot_manager": ["connector-development", "qa"],
    "admin": ["connector-development", "qa", "uat"],
}

# Default GitHub repo for shielva-connectors
DEFAULT_GITHUB_REPO = "git@github.com:shielvaAdmin/shielva-connectors"

SYNC_PERMISSIONS: Dict[str, Dict[str, bool]] = {
    # Platform identity roles (issued by the identity service in the JWT `role` claim)
    "platform_owner": {"can_raise": True, "can_approve": True, "can_hold": True, "can_configure_approvers": True},
    "org_owner": {"can_raise": True, "can_approve": True, "can_hold": True, "can_configure_approvers": True},
    # Sync-route operational roles
    "super_admin": {"can_raise": True, "can_approve": True, "can_hold": True, "can_configure_approvers": True},
    "tenant_admin": {"can_raise": True, "can_approve": True, "can_hold": True, "can_configure_approvers": True},
    "bot_manager": {"can_raise": True, "can_approve": False, "can_hold": False, "can_configure_approvers": False},
    "admin": {"can_raise": True, "can_approve": True, "can_hold": True, "can_configure_approvers": True},
    "analyst": {"can_raise": False, "can_approve": False, "can_hold": False, "can_configure_approvers": False},
    "viewer": {"can_raise": False, "can_approve": False, "can_hold": False, "can_configure_approvers": False},
    "partner": {"can_raise": False, "can_approve": False, "can_hold": False, "can_configure_approvers": False},
}

# Maximum payload size for /raise (25 MB)
MAX_SYNC_PAYLOAD_BYTES = 25 * 1024 * 1024

# ── SSE broadcast ────────────────────────────────────────────────────────────
# In-memory per-tenant SSE queues. Each connected client gets its own queue.
# Keyed by tenant_id → set of asyncio.Queue instances.

_sse_clients: Dict[str, set] = {}

# ── Active CI task registry ───────────────────────────────────────────────────
# Maps sync_request_id → asyncio.Task for the running CI pipeline.
# Used by the cancel-ci endpoint to cancel any in-progress gate (including the
# long-running SDK security scan). Entries are removed automatically when the
# task finishes via a done-callback.

_ci_tasks: Dict[str, "asyncio.Task[Any]"] = {}


def _broadcast(tenant_id: str, event: str, data: Any) -> None:
    """Push an SSE event to all connected clients for a tenant."""
    payload = json.dumps(data, default=str)
    clients = _sse_clients.get(tenant_id, set())
    dead = []
    for q in clients:
        try:
            q.put_nowait(f"event: {event}\ndata: {payload}\n\n")
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        clients.discard(q)


@sync_request_router.post("/connector-sync")
async def connector_sync_broadcast(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    body: dict = Body(...),
):
    """Internal endpoint — called by CMS core after connector CRUD to broadcast change to all SSE clients."""
    action = body.get("action", "changed")  # created | updated | deleted
    _broadcast(x_tenant_id, f"connector:{action}", body)
    return {"ok": True}


# ── MongoDB collections ─────────────────────────────────────────────────────

def _sync_requests_col():
    return get_db()["sync_requests"]


def _sync_settings_col():
    return get_db()["sync_settings"]


async def ensure_sync_indexes() -> None:
    """Create MongoDB indexes for sync collections. Idempotent — safe to call on every boot."""
    req_col = _sync_requests_col()
    await req_col.create_index([("tenant_id", 1), ("status", 1)])
    await req_col.create_index([("tenant_id", 1), ("created_at", -1)])
    await req_col.create_index([("pr_number", 1), ("tenant_id", 1)])

    settings_col = _sync_settings_col()
    await settings_col.create_index("tenant_id", unique=True)
    logger.info("sync.indexes_ensured")


# ── Token encryption helpers ────────────────────────────────────────────────
# Encrypt GitHub PAT at rest in MongoDB using Fernet symmetric encryption.

_fernet = None


def _get_fernet():
    """Lazy-init Fernet cipher from SYNC_TOKEN_ENCRYPTION_KEY."""
    global _fernet
    if _fernet is None:
        key = settings.SYNC_TOKEN_ENCRYPTION_KEY
        if not key:
            return None
        from cryptography.fernet import Fernet
        # Key must be 32 url-safe base64-encoded bytes. If the user provided a
        # plain passphrase, derive a proper key via SHA-256 + base64.
        import base64
        if len(key) == 44 and key.endswith("="):
            # Looks like a valid Fernet key already
            _fernet = Fernet(key.encode())
        else:
            # Derive a Fernet-compatible key from the passphrase
            raw = hashlib.sha256(key.encode()).digest()
            _fernet = Fernet(base64.urlsafe_b64encode(raw))
    return _fernet


def _encrypt_token(token: str) -> str:
    """Encrypt a token for storage. Falls back to plain text if no key configured."""
    f = _get_fernet()
    if not f or not token:
        return token
    return f.encrypt(token.encode()).decode()


def _decrypt_token(stored: str) -> str:
    """Decrypt a stored token. Falls back to plain text if decryption fails (e.g. legacy unencrypted value)."""
    f = _get_fernet()
    if not f or not stored:
        return stored
    try:
        return f.decrypt(stored.encode()).decode()
    except Exception:
        # Legacy unencrypted value or wrong key — return as-is
        return stored


# ── Request / Response models ────────────────────────────────────────────────

class SyncFilePayload(BaseModel):
    path: str
    content: str


class RaiseSyncRequestBody(BaseModel):
    session_id: str
    connector_name: str
    target_branch: str
    files: List[SyncFilePayload]


class ApproveSyncRequestBody(BaseModel):
    pass  # no body needed — auth from headers


class RerunSyncRequestBody(BaseModel):
    run_all: bool = False  # when True: run all gates without early-exit, always include integration tests


class RaisePrBody(BaseModel):
    branch_name: Optional[str] = None  # if set, overrides the branch recorded on the sync request


class SyncSettingsBody(BaseModel):
    github_repo_url: Optional[str] = None
    github_token: Optional[str] = None
    default_target_branch: Optional[str] = None
    authorized_approvers: Optional[List[str]] = None


# ── Security checks (CI Gate 1) ─────────────────────────────────────────────

SECRET_PATTERNS = [
    re.compile(r"""(?:api[_-]?key|secret|password|token|auth)\s*[:=]\s*['"][A-Za-z0-9+/=_\-]{16,}['"]""", re.I),
    re.compile(r"""(?:aws_access_key_id|aws_secret_access_key)\s*=\s*\S+""", re.I),
    re.compile(r"""-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"""),
    re.compile(r"""ghp_[A-Za-z0-9]{36}"""),  # GitHub PAT
    re.compile(r"""sk-[A-Za-z0-9]{20,}"""),  # OpenAI / Anthropic keys
]

DANGEROUS_CALLS = re.compile(r"""\b(?:eval|exec|os\.system|subprocess\.call|subprocess\.Popen)\s*\(""")

JUNK_PATTERNS = [
    "__pycache__/", ".pyc", ".pyo", ".DS_Store", ".idea/", ".vscode/",
    "Thumbs.db", ".env", ".env.local", "node_modules/",
]


def _write_files_to_tempdir(files: List["SyncFilePayload"]) -> Path:
    """Write sync request files into a fresh temp directory.

    Strips the leading 'generated_connectors/{name}/' prefix so that
    connector.py / tests/ land at the root of the returned directory.
    Never uses hardcoded paths — tempfile.mkdtemp() is used for isolation.
    """
    tmp = Path(tempfile.mkdtemp(prefix="shielva_ci_"))
    # Detect common prefix: generated_connectors/{connector_name}/
    prefix = ""
    if files:
        first = files[0].path.replace("\\", "/")
        parts = first.split("/")
        if len(parts) >= 2 and parts[0] == "generated_connectors":
            prefix = f"{parts[0]}/{parts[1]}/"
    for f in files:
        rel = f.path.replace("\\", "/")
        if prefix and rel.startswith(prefix):
            rel = rel[len(prefix):]
        dest = tmp / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f.content, encoding="utf-8")
    return tmp


def _validate_path(rel_path: str) -> bool:
    """Return True if the path is safe (no traversal, no absolute)."""
    if ".." in rel_path or rel_path.startswith("/") or rel_path.startswith("\\"):
        return False
    return True


def _security_audit_sync(files: List[dict]) -> Dict[str, Any]:
    """Gate 1: Scan files for secrets, dangerous calls, credential files.

    This is the CPU-bound version that runs in a thread pool.
    Accepts dicts (not Pydantic models) because it's called via run_in_executor.
    """
    findings = []
    for f in files:
        path = f["path"]
        content = f["content"]
        if not _validate_path(path):
            findings.append({"file": path, "issue": "Path traversal attempt", "severity": "critical"})
            continue
        basename = os.path.basename(path).lower()
        if basename in (".env", ".env.local", ".env.production", "credentials.json", "secrets.json"):
            findings.append({"file": path, "issue": f"Credential file detected: {basename}", "severity": "critical"})
            continue
        for i, line in enumerate(content.split("\n"), 1):
            for pat in SECRET_PATTERNS:
                if pat.search(line):
                    findings.append({"file": path, "line": i, "issue": "Potential hardcoded secret", "severity": "high"})
                    break
            if DANGEROUS_CALLS.search(line):
                findings.append({"file": path, "line": i, "issue": "Dangerous function call (eval/exec/os.system)", "severity": "high"})

    passed = not any(f["severity"] == "critical" for f in findings)
    return {
        "gate": "security_audit",
        "status": "passed" if passed else "failed",
        "summary": f"{len(findings)} finding(s)" if findings else "No security issues detected",
        "details": json.dumps(findings) if findings else None,
    }


async def _security_audit(files: List[SyncFilePayload]) -> Dict[str, Any]:
    """Gate 1 async wrapper — offloads CPU-bound regex scanning to a thread pool."""
    file_dicts = [{"path": f.path, "content": f.content} for f in files]
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(_security_audit_sync, file_dicts))


async def _enrich_findings_with_ai(findings: List[Dict]) -> List[Dict]:
    """Call shielva-security-ai /api/v1/enrich/batch to add AI fix suggestions.

    Returns the findings list with ai_fix populated. Never raises — on error
    the original findings are returned unchanged.
    """
    if not findings:
        return findings

    # security-ai runs on the same host as security-api but port 8043 with SSL.
    ai_url = settings.SHIELVA_SECURITY_URL.replace(":8045", ":8043")
    # Allow explicit override via SHIELVA_SECURITY_AI_URL env var
    ai_url = getattr(settings, "SHIELVA_SECURITY_AI_URL", None) or ai_url

    # Build payload — give each finding a stable finding_id
    payload_findings = []
    for i, f in enumerate(findings):
        payload_findings.append({
            "finding_id":    f.get("id") or f.get("finding_id") or str(i),
            "title":         f.get("issue") or f.get("title") or "Security finding",
            "severity":      (f.get("severity") or "medium").upper(),
            "file_path":     f.get("file") or f.get("file_path"),
            "line_start":    f.get("line") or f.get("line_number"),
            "description":   f.get("description") or "",
            "scanner":       f.get("scanner") or "shielva-security",
            "code_snippet":  f.get("code_snippet") or "",
        })

    try:
        async with httpx.AsyncClient(verify=False, timeout=120.0) as client:
            resp = await client.post(
                f"{ai_url}/api/v1/enrich/batch",
                json={"findings": payload_findings},
            )
            resp.raise_for_status()
            results: List[Dict] = resp.json().get("results", [])

        # Map enrichment results back by finding_id
        enrich_map = {r["finding_id"]: r for r in results}
        enriched = []
        for i, f in enumerate(findings):
            fid = f.get("id") or f.get("finding_id") or str(i)
            r = enrich_map.get(fid) or enrich_map.get(str(i), {})
            enriched.append({
                **f,
                "ai_fix":        r.get("fix_suggestion"),
                "ai_explanation": r.get("explanation"),
            })
        return enriched
    except Exception as exc:
        logger.warning("security_ai_enrich_failed", ai_url=ai_url, error=str(exc)[:300])
        return findings


async def _security_audit_sdk(
    sync_request_id: str,
    branch_name: str,
    commit_sha: str,
    sync_settings: Dict,
    files: Optional[List[Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Gate 1 via shielva-security-sdk — full scan, identical to the stepper flow.

    Strategy (local-first, mirrors vuln_scan_service):
      1. Write connector source files to a local temp dir.
      2. Pass the local path directly to the security-api (scan_type="full").
         This gives SAST + SCA + Secrets + IaC — the same breadth as the stepper.
         No GitHub clone roundtrip needed, no orphan-branch gymnastics.
      3. Fetch findings, enrich with AI fix suggestions.
      4. Clean up temp dir in finally.

    Fallback chain:
      - No api_key  → return None (regex scanner)
      - No files    → try GitHub URL scan (branch_name / commit_sha)
      - SDK error   → return None (regex scanner)

    Gate decision:
      - "failed"  → any CRITICAL finding (blocks merge)
      - "passed"  → HIGH / MEDIUM / LOW (informational, merge allowed)
    """
    api_key = settings.SHIELVA_SECURITY_API_KEY

    if not api_key or not api_key.startswith("shv_sk_"):
        logger.info("security_sdk_skipped", reason="no valid SHIELVA_SECURITY_API_KEY")
        return None

    repo_url = sync_settings.get("github_repo_url", "")
    gh_token = sync_settings.get("github_token", "")

    client = None
    _scan_tmpdir: Optional[Path] = None
    _scan_branch_pushed = False
    _owner = _repo_name = ""

    try:
        from shielva_security import ShielvaSecurityClient
        from shielva_security.exceptions import ScanFailedError, ScanTimeoutError

        client = ShielvaSecurityClient(
            api_key=api_key,
            base_url=settings.SHIELVA_SECURITY_URL,
            timeout=60.0,
        )

        # ── Determine scan target ──────────────────────────────────────────────
        # Preferred: local temp dir — same approach as vuln_scan_service.
        # The security-api and connectors service run on the same host, so a
        # local path avoids the GitHub clone roundtrip and enables full scanning.
        scan_target: str
        scan_kwargs: Dict[str, Any] = {}

        if files:
            # Write ONLY connector source files (no tests, no fixtures)
            def _is_source_file(path: str) -> bool:
                norm = path.replace("\\", "/")
                parts = norm.split("/")
                fn = parts[-1]
                if "tests" in parts or "test" in parts:
                    return False
                if fn.startswith("test_") or fn in ("conftest.py", "pytest.ini"):
                    return False
                if fn.endswith(".py"):
                    return True
                if fn.startswith("requirements") and fn.endswith(".txt"):
                    return True
                return False

            source_files = [f for f in files if _is_source_file(f.path)] or list(files)
            _scan_tmpdir = _write_files_to_tempdir(source_files)
            scan_target = str(_scan_tmpdir)
            logger.info(
                "security_sdk_local_scan",
                sync_request_id=sync_request_id,
                target=scan_target,
                file_count=len(source_files),
            )

        elif repo_url:
            # Fallback: no files in memory → scan GitHub branch directly
            if repo_url.startswith("git@github.com:"):
                repo_url = "https://github.com/" + repo_url.replace("git@github.com:", "").rstrip(".git")
            if not repo_url.startswith("https://github.com"):
                logger.warning("security_sdk_skipped", reason="invalid repo_url", url=repo_url[:60])
                return None

            # Sync PAT for cloning private repos
            if gh_token:
                try:
                    await client.settings.update(github_token=gh_token)
                except Exception as tok_err:
                    logger.warning("security_sdk_token_sync_failed", error=str(tok_err)[:200])

            # Push orphan scan branch (connector source only)
            try:
                _url_parts = repo_url.rstrip("/").rstrip(".git").split("github.com/")[-1].split("/")
                _owner, _repo_name = _url_parts[0], _url_parts[1]
                scan_branch = f"security-scan-{sync_request_id[-12:]}"
                await _push_security_scan_branch(_owner, _repo_name, gh_token, scan_branch, files or [])
                _scan_branch_pushed = True
                scan_kwargs = {"branch": scan_branch, "commit_sha": None}
            except Exception as push_err:
                logger.warning("security_sdk_scan_branch_push_failed", error=str(push_err)[:200])
                return None

            scan_target = repo_url
            logger.info(
                "security_sdk_github_scan",
                sync_request_id=sync_request_id,
                repo=repo_url,
                branch=scan_branch,
            )
        else:
            logger.info("security_sdk_skipped", reason="no files and no repo_url")
            return None

        # ── Trigger full scan (SAST + SCA + Secrets + IaC) ────────────────────
        # "full" matches what vuln_scan_service uses in the stepper flow.
        timeout = settings.SHIELVA_SECURITY_SCAN_TIMEOUT
        scan = await client.scans.create_and_wait(
            target=scan_target,
            scan_type="full",
            timeout=timeout,
            **scan_kwargs,
        )

        # ── Fetch findings ─────────────────────────────────────────────────────
        findings_raw = await client.scans.findings(scan.id)
        findings = [
            {
                "id":           f.id,
                "file":         f.file_path or "",
                "line":         f.line_number,
                "issue":        f.title,
                "severity":     f.severity.lower(),
                "description":  f.description or "",
                "scanner":      f.scanner,
                "rule_id":      f.rule_id or "",
                "code_snippet": f.code_snippet or None,
                # Extra fields matching stepper/vuln_scan_service output
                "fix_guidance": f.remediation or "",
                "cwe":          [f.cwe] if f.cwe else [],
                "owasp":        [f.owasp] if f.owasp else [],
                "package":      f.package_name or "",
                "fix_version":  f.fix_version or "",
            }
            for f in findings_raw
        ]

        logger.info(
            "security_sdk_scan_done",
            sync_request_id=sync_request_id,
            scan_id=scan.id,
            scan_type="full",
            finding_count=len(findings),
            mode="local" if _scan_tmpdir else "github",
        )

        # ── AI enrichment ──────────────────────────────────────────────────────
        if findings:
            findings = await _enrich_findings_with_ai(findings)

        # ── Gate decision: CRITICAL = fail, else pass (informational) ──────────
        has_critical = any(f["severity"].lower() == "critical" for f in findings)
        sev_counts: Dict[str, int] = {}
        for f in findings:
            sev_counts[f["severity"].upper()] = sev_counts.get(f["severity"].upper(), 0) + 1

        _SEV_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        summary_parts = [f"{sev_counts[s]} {s}" for s in _SEV_ORDER if sev_counts.get(s)]
        summary = (
            ("Security scan FAILED — " if has_critical else "Security scan passed — ")
            + ", ".join(summary_parts) + " finding(s)"
            if summary_parts else "No security issues detected"
        )

        return {
            "gate":    "security_audit",
            "status":  "failed" if has_critical else "passed",
            "summary": summary,
            "details": json.dumps(findings) if findings else None,
        }

    except ScanTimeoutError:
        logger.warning("security_sdk_timeout", sync_request_id=sync_request_id, timeout=settings.SHIELVA_SECURITY_SCAN_TIMEOUT)
        return {
            "gate":    "security_audit",
            "status":  "passed",
            "summary": f"Security scan timed out after {settings.SHIELVA_SECURITY_SCAN_TIMEOUT}s — review manually",
            "details": None,
        }
    except ScanFailedError as exc:
        logger.warning("security_sdk_scan_failed", error=str(exc), sync_request_id=sync_request_id)
        return None
    except Exception as exc:
        logger.warning("security_sdk_error", error=str(exc)[:300], sync_request_id=sync_request_id)
        return None
    finally:
        # Clean up local scan temp dir
        if _scan_tmpdir and _scan_tmpdir.exists():
            shutil.rmtree(_scan_tmpdir, ignore_errors=True)
        # Clean up ephemeral GitHub scan branch (fallback path only)
        if _scan_branch_pushed and gh_token and _owner and _repo_name:
            try:
                await _delete_branch(_owner, _repo_name, gh_token, f"security-scan-{sync_request_id[-12:]}")
            except Exception:
                pass
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


async def _delete_ci_branch(branch_name: str, sync_settings: Dict) -> None:
    """Delete a CI branch from GitHub after a failed pipeline run.

    Called when CI gates fail or an unrecoverable error occurs, so that
    branches pushed purely for security scanning don't pile up on the repo.
    Non-fatal — logs a warning on failure but never raises.
    """
    if not branch_name or not sync_settings:
        return
    repo_url = sync_settings.get("github_repo_url", "")
    token    = sync_settings.get("github_token", "")
    if not repo_url or not token:
        return
    try:
        owner, repo = _parse_repo(repo_url)
        await _delete_branch(owner, repo, token, branch_name)
        logger.info("ci_branch_deleted", branch=branch_name)
    except Exception as exc:
        logger.warning("ci_branch_delete_failed", branch=branch_name, error=str(exc)[:200])


def _smart_diff(files: List[SyncFilePayload], drop_empty: bool = True) -> tuple[List[SyncFilePayload], Dict[str, Any]]:
    """Gate 2: Strip junk files (pycache, .pyc, IDE configs, etc.).

    drop_empty: when True (sync push), whitespace-only files are also stripped.
    For DIFF comparisons pass drop_empty=False — empty files (e.g. package
    __init__.py) genuinely live on the branch, so dropping them only on the local
    side makes them look falsely "removed" and produces phantom diffs.
    """
    cleaned = []
    removed = []
    for f in files:
        is_junk = False
        for pat in JUNK_PATTERNS:
            if pat in f.path.lower():
                is_junk = True
                removed.append(f.path)
                break
        if is_junk:
            continue
        if drop_empty and not f.content.strip():
            removed.append(f.path)
        else:
            cleaned.append(f)

    result = {
        "gate": "smart_diff",
        "status": "passed",
        "summary": f"Kept {len(cleaned)} file(s), stripped {len(removed)} junk/empty file(s)",
        "details": json.dumps({"removed": removed}) if removed else None,
    }
    return cleaned, result


# ── GitHub API helpers ───────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com"

# Module-level persistent httpx client for GitHub API — connection pooling
_gh_client: Optional[httpx.AsyncClient] = None


def _get_gh_client() -> httpx.AsyncClient:
    """Get or create a persistent GitHub API client with connection pooling."""
    global _gh_client
    if _gh_client is None or _gh_client.is_closed:
        _gh_client = httpx.AsyncClient(
            base_url=GITHUB_API,
            timeout=30.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _gh_client


async def close_gh_client() -> None:
    """Close the GitHub client — call from app shutdown."""
    global _gh_client
    if _gh_client and not _gh_client.is_closed:
        await _gh_client.aclose()
        _gh_client = None


def _parse_repo(repo_url: str) -> tuple[str, str]:
    """Extract owner/repo from a GitHub URL.

    Supports both formats:
      - https://github.com/org/repo
      - git@github.com:org/repo.git
    """
    url = repo_url.strip()
    # SSH format: git@github.com:owner/repo.git
    if url.startswith("git@"):
        path = url.split(":")[-1].rstrip(".git")
        parts = path.split("/")
        if len(parts) >= 2:
            return parts[0], parts[1]
    # HTTPS format: https://github.com/org/repo
    url = url.rstrip("/").rstrip(".git")
    parts = url.split("/")
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    raise ValueError(f"Invalid GitHub repo URL: {repo_url}")


async def _github_request(
    method: str, path: str, token: str,
    json_body: Optional[Dict] = None,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> Dict:
    """Make an authenticated GitHub API request with retry/backoff.

    Retries on 5xx and 429 (rate-limit) with exponential backoff.
    Uses the persistent connection-pooled client.
    """
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    client = _get_gh_client()

    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            resp = await client.request(
                method,
                path,  # base_url already set on client
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
            # Retry on server errors and rate limits
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning("github_api_retry", status=resp.status_code, path=path, attempt=attempt, wait_s=round(wait, 1))
                await asyncio.sleep(wait)
                continue

            if resp.status_code >= 400:
                logger.error("github_api_error", status=resp.status_code, path=path, body=resp.text[:500])
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"GitHub API error: {resp.status_code} — {resp.text[:200]}",
                )
            return resp.json() if resp.text else {}

        except HTTPException:
            raise
        except Exception as e:
            last_exc = e
            if attempt < max_retries - 1:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning("github_api_network_retry", error=str(e)[:100], path=path, attempt=attempt, wait_s=round(wait, 1))
                await asyncio.sleep(wait)
            else:
                logger.error("github_api_exhausted", error=str(e)[:200], path=path, attempts=max_retries)
                raise HTTPException(status_code=502, detail=f"GitHub API unreachable after {max_retries} retries: {str(e)[:100]}")

    # Should not reach here, but just in case
    raise HTTPException(status_code=502, detail=f"GitHub API request failed: {last_exc}")


async def _push_branch(
    owner: str, repo: str, token: str,
    branch_name: str, target_branch: str,
    files: List[SyncFilePayload],
    commit_message: str = "Shielva sync — automated commit",
) -> str:
    """Push connector files onto a new GitHub branch. Returns the commit SHA.

    Creates blobs (parallel), tree, commit, and branch ref.
    If the branch ref already exists (e.g. CI re-run), updates it by force.
    """
    repo_path = f"/repos/{owner}/{repo}"

    # 1. Get target branch SHA
    ref_data = await _github_request("GET", f"{repo_path}/git/ref/heads/{target_branch}", token)
    base_sha = ref_data["object"]["sha"]

    # 2. Get the base tree
    commit_data = await _github_request("GET", f"{repo_path}/git/commits/{base_sha}", token)
    base_tree_sha = commit_data["tree"]["sha"]

    # 3. Create blobs for each file — PARALLEL via asyncio.gather()
    async def _create_blob(f: SyncFilePayload) -> Dict:
        blob = await _github_request("POST", f"{repo_path}/git/blobs", token, {
            "content": f.content,
            "encoding": "utf-8",
        })
        return {
            "path": f.path,
            "mode": "100644",
            "type": "blob",
            "sha": blob["sha"],
        }

    tree_items = await asyncio.gather(*[_create_blob(f) for f in files])

    # 4. Create tree
    tree = await _github_request("POST", f"{repo_path}/git/trees", token, {
        "base_tree": base_tree_sha,
        "tree": list(tree_items),
    })

    # 5. Create commit
    commit = await _github_request("POST", f"{repo_path}/git/commits", token, {
        "message": commit_message,
        "tree": tree["sha"],
        "parents": [base_sha],
    })
    commit_sha = commit["sha"]

    # 6. Create branch ref (or update if it already exists from a re-run)
    try:
        await _github_request("POST", f"{repo_path}/git/refs", token, {
            "ref": f"refs/heads/{branch_name}",
            "sha": commit_sha,
        })
    except HTTPException as e:
        if e.status_code == 422:
            # Branch already exists (re-run scenario) — force-update it
            await _github_request("PATCH", f"{repo_path}/git/refs/heads/{branch_name}", token, {
                "sha": commit_sha,
                "force": True,
            })
        else:
            raise

    return commit_sha


async def _push_security_scan_branch(
    owner: str, repo: str, token: str,
    scan_branch: str,
    files: List["SyncFilePayload"],
) -> str:
    """Push ONLY connector source files to a clean orphan branch for security scanning.

    Unlike _push_branch, this does NOT use base_tree — the resulting branch
    contains EXACTLY the connector source files and nothing else.  No inherited
    history from connector-development, no test fixtures, no generated configs.

    Branch is meant to be ephemeral: _security_audit_sdk deletes it in its
    finally block regardless of scan outcome.

    Returns the commit SHA.
    """
    repo_path = f"/repos/{owner}/{repo}"

    # ── Filter: keep only connector source files ──────────────────────────────
    # Tests, conftest, pytest.ini, yaml fixtures, etc. are noise for SAST.
    # We want the production Python source that the connector runs in prod.
    def _is_source_file(path: str) -> bool:
        norm = path.replace("\\", "/")
        parts = norm.split("/")
        filename = parts[-1]
        # Exclude test directories entirely
        if "tests" in parts or "test" in parts:
            return False
        # Exclude individual test/config files
        if filename.startswith("test_") or filename in ("conftest.py", "pytest.ini"):
            return False
        # Include Python source
        if filename.endswith(".py"):
            return True
        # Include requirements (SCA — dependency vulnerability detection)
        if filename.startswith("requirements") and filename.endswith(".txt"):
            return True
        return False

    scan_files = [f for f in files if _is_source_file(f.path)]
    if not scan_files:
        # Nothing survived the filter — fall back to all files (shouldn't happen
        # in normal usage but keeps the gate functional)
        scan_files = files
        logger.warning("security_scan_branch_no_filter_match", total=len(files))

    logger.info(
        "security_scan_branch_files",
        scan_branch=scan_branch,
        total_files=len(files),
        scan_files=len(scan_files),
    )

    # ── Create blobs in parallel ───────────────────────────────────────────────
    async def _create_blob(f: "SyncFilePayload") -> Dict:
        blob = await _github_request("POST", f"{repo_path}/git/blobs", token, {
            "content": f.content,
            "encoding": "utf-8",
        })
        return {"path": f.path, "mode": "100644", "type": "blob", "sha": blob["sha"]}

    tree_items = await asyncio.gather(*[_create_blob(f) for f in scan_files])

    # ── Create tree with NO base_tree (clean slate) ────────────────────────────
    tree = await _github_request("POST", f"{repo_path}/git/trees", token, {
        "tree": list(tree_items),
        # Deliberately NO "base_tree" — orphan tree, only our files
    })

    # ── Orphan commit (no parents) ─────────────────────────────────────────────
    commit = await _github_request("POST", f"{repo_path}/git/commits", token, {
        "message": f"security-scan: connector source snapshot ({scan_branch})",
        "tree": tree["sha"],
        "parents": [],
    })
    commit_sha = commit["sha"]

    # ── Create branch ref ──────────────────────────────────────────────────────
    await _github_request("POST", f"{repo_path}/git/refs", token, {
        "ref": f"refs/heads/{scan_branch}",
        "sha": commit_sha,
    })

    return commit_sha


async def _open_pr(
    owner: str, repo: str, token: str,
    branch_name: str, target_branch: str,
    title: str, body: str,
) -> Dict[str, Any]:
    """Open a GitHub PR from an existing branch. Returns {pr_number, pr_url, branch_name}."""
    repo_path = f"/repos/{owner}/{repo}"
    pr = await _github_request("POST", f"{repo_path}/pulls", token, {
        "title": title,
        "body": body,
        "head": branch_name,
        "base": target_branch,
    })
    return {
        "pr_number": pr["number"],
        "pr_url": pr["html_url"],
        "branch_name": branch_name,
    }


async def _create_pr(
    owner: str, repo: str, token: str,
    branch_name: str, target_branch: str,
    title: str, body: str,
    files: List[SyncFilePayload],
) -> Dict[str, Any]:
    """Create a branch, commit files, and open a PR. Returns PR data.

    Convenience wrapper around _push_branch + _open_pr.
    Used only when raise_pr is called without a prior CI branch push.
    """
    commit_sha = await _push_branch(owner, repo, token, branch_name, target_branch, files, title)
    pr_data = await _open_pr(owner, repo, token, branch_name, target_branch, title, body)
    return {
        "pr_number": pr_data["pr_number"],
        "pr_url": pr_data["pr_url"],
        "branch_name": branch_name,
        "commit_sha": commit_sha,
    }


async def _get_pr_diff(owner: str, repo: str, token: str, pr_number: int) -> List[Dict]:
    """Fetch file-by-file diff from a PR."""
    files = await _github_request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/files", token)
    return [
        {
            "filename": f["filename"],
            "status": f["status"],
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "patch": f.get("patch", ""),
        }
        for f in files
    ]


async def _merge_pr(owner: str, repo: str, token: str, pr_number: int) -> Dict:
    """Merge a PR with squash."""
    return await _github_request("PUT", f"/repos/{owner}/{repo}/pulls/{pr_number}/merge", token, {
        "merge_method": "squash",
    })


async def _delete_branch(owner: str, repo: str, token: str, branch_name: str) -> None:
    """Delete a feature branch after merge."""
    try:
        await _github_request("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{branch_name}", token)
    except Exception:
        pass  # Non-critical — branch may already be deleted


async def _close_pr(owner: str, repo: str, token: str, pr_number: int) -> None:
    """Close a PR without merging."""
    try:
        await _github_request("PATCH", f"/repos/{owner}/{repo}/pulls/{pr_number}", token, {
            "state": "closed",
        })
    except Exception:
        pass


# ── Tenant sync settings helpers ─────────────────────────────────────────────

async def _get_tenant_sync_settings(tenant_id: str) -> Dict:
    """Get or create sync settings for a tenant. Decrypts the GitHub token."""
    col = _sync_settings_col()
    doc = await col.find_one({"tenant_id": tenant_id})
    if not doc:
        return {
            "tenant_id": tenant_id,
            "github_repo_url": DEFAULT_GITHUB_REPO,
            "github_token": "",
            "default_target_branch": "connector-development",
            "authorized_approvers": [],
        }
    doc["_id"] = str(doc["_id"])
    # Decrypt token on read
    if doc.get("github_token"):
        doc["github_token"] = _decrypt_token(doc["github_token"])
    return doc


# ── Cancellable subprocess helpers ───────────────────────────────────────────

async def _run_pytest_cancellable(
    out_dir: Path,
    test_mode: str = "unit",
) -> Dict[str, Any]:
    """Run pytest as a real subprocess (asyncio.create_subprocess_exec).

    Unlike asyncio.to_thread(subprocess.run, ...), this coroutine is truly
    cancellable: if the outer CI task receives CancelledError, the subprocess
    is killed before re-raising, so no orphan pytest process is left running.

    Returns a dict with keys: passed, failed, errors, skipped, details, output.
    """
    import sysconfig as _sc2, site as _st2
    _site_pkgs2 = _sc2.get_paths().get("purelib", "")
    _user_site2 = _st2.getusersitepackages() if hasattr(_st2, "getusersitepackages") else ""
    import sys as _sys2
    repo_root = Path(os.environ.get("GENERATED_CODE_DIR", str(out_dir.parent.parent))).resolve().parent
    python_path = os.pathsep.join(filter(None, [
        str(out_dir), str(out_dir.parent), _site_pkgs2, _user_site2, str(repo_root),
    ]))

    tests_dir = out_dir / "tests"

    # Ensure conftest.py with asyncio_mode=auto
    conftest = tests_dir / "conftest.py"
    if tests_dir.exists() and not conftest.exists():
        conftest.write_text(
            "import pytest\n\n"
            "def pytest_configure(config):\n"
            "    config.addinivalue_line('markers', 'asyncio: mark test as async')\n",
            encoding="utf-8",
        )

    # Ensure pytest.ini
    pytest_ini = out_dir / "pytest.ini"
    if not pytest_ini.exists():
        pytest_ini.write_text("[pytest]\nasyncio_mode = auto\ntimeout = 60\n", encoding="utf-8")

    # If no test files found, report as skipped
    if not tests_dir.exists() or not list(tests_dir.glob("test_*.py")):
        return {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "details": [], "output": "No test files found"}

    cov_json = out_dir / ".coverage_report.json"
    run_cov = (test_mode == "full")

    cmd = [
        _sys2.executable, "-m", "pytest",
        str(tests_dir),
        "-v", "--tb=short", "--no-header",
        f"--rootdir={out_dir}",
    ]
    if run_cov:
        cmd += [
            f"--cov={out_dir}",
            "--cov-report=term-missing",
            f"--cov-report=json:{cov_json}",
            "--cov-config=/dev/null",
        ]

    env = {**os.environ, "PYTHONPATH": python_path}

    async def _exec(command: list) -> tuple:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(out_dir),
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except (asyncio.TimeoutError, asyncio.CancelledError) as _kill_exc:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            raise _kill_exc
        return stdout.decode(errors="replace"), stderr.decode(errors="replace"), proc.returncode

    stdout, stderr, returncode = await _exec(cmd)
    output = stdout + stderr

    # Retry without --cov if coverage flags caused failure
    if run_cov and returncode != 0 and (
        "unrecognized arguments" in output or "no module named pytest_cov" in output.lower()
    ):
        cmd_no_cov = [c for c in cmd if not c.startswith("--cov")]
        stdout, stderr, returncode = await _exec(cmd_no_cov)
        output = stdout + stderr
        run_cov = False

    passed = output.count(" PASSED")
    failed = output.count(" FAILED")
    skipped = output.count(" SKIPPED")
    errors = output.count("ERROR collecting") + output.count("ERROR tests/")

    details = []
    for line in output.split("\n"):
        if " PASSED" in line or " FAILED" in line or " SKIPPED" in line:
            parts = line.strip().split(" ")
            if len(parts) >= 2:
                test_name = parts[0]
                status = "passed" if "PASSED" in line else ("failed" if "FAILED" in line else "skipped")
                details.append({"test": test_name, "status": status})
        elif "ERROR collecting" in line or ("ERROR " in line and "tests/" in line):
            details.append({"test": line.strip(), "status": "error"})

    # Parse per-test failure messages
    import re as _re2
    _failure_msgs: dict = {}
    _cur_test: Optional[str] = None
    _cur_lines: list = []
    for _line in output.split("\n"):
        _hdr = _re2.match(r"^_{5,}\s+(\S+)\s+_{5,}", _line)
        if _hdr:
            if _cur_test and _cur_lines:
                _failure_msgs[_cur_test] = "\n".join(_cur_lines)
            _cur_test = _hdr.group(1).split("::")[-1]
            _cur_lines = []
        elif _cur_test:
            if _line.startswith("E ") or _line.startswith("E\t"):
                _cur_lines.append(_line[2:].strip())
            elif _line.startswith("======"):
                if _cur_test and _cur_lines:
                    _failure_msgs[_cur_test] = "\n".join(_cur_lines)
                _cur_test = None
                _cur_lines = []
    if _cur_test and _cur_lines:
        _failure_msgs[_cur_test] = "\n".join(_cur_lines)
    for _d in details:
        if _d["status"] == "failed":
            _fn = _d["test"].split("::")[-1] if "::" in _d["test"] else _d["test"]
            if _fn in _failure_msgs:
                _d["message"] = _failure_msgs[_fn]

    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "details": details,
        "output": (output[:6000] + "\n...\n" + output[-2000:]) if len(output) > 8000 else output,
    }


# ── CI pipeline runner ───────────────────────────────────────────────────────

async def _run_ci_pipeline(
    sync_request_id: str,
    tenant_id: str,
    session_id: str,
    files: List[SyncFilePayload],
    run_all: bool = False,
    branch_name: Optional[str] = None,
    sync_settings: Optional[Dict] = None,
) -> tuple[List[Dict], List[SyncFilePayload]]:
    """Run all CI gates sequentially. Broadcasts SSE progress. Returns (ci_results, cleaned_files).

    Test gates respect the session's ``test_type`` field:
      - "unit"  → only run unit tests (Gate 4)
      - "both"  → run unit tests (Gate 4) + integration tests (Gate 5)
    If test_type is not set, defaults to "unit" only.

    When ``branch_name`` and ``sync_settings`` are provided, Gate 1 pushes the
    connector files to a GitHub branch first and then scans via
    shielva-security-sdk. Falls back to the local regex scanner if the SDK
    is not configured or the scan fails.
    """
    col = _sync_requests_col()
    oid = ObjectId(sync_request_id)
    ci_results = []

    # Fetch session to determine test_type
    from integration.db.database import sessions_collection
    session_doc = await sessions_collection().find_one(
        {"_id": ObjectId(session_id)},
        {"test_type": 1},
    )
    test_type = (session_doc or {}).get("test_type", "unit")  # "unit" or "both"

    # ci_results_details_by_gate accumulates the heavy `details` JSON dumps
    # per gate. We strip `details` from what we $set into Mongo (saves ~10–50
    # KB per gate × ~6 gates) and shuttle the dumps to R2 instead so the
    # diff modal can fetch them in one call when the user opens it.
    ci_results_details_by_gate: Dict[str, str] = {}

    async def _update_gate(gate_result: Dict):
        ci_results.append(gate_result)

        # Slim copy for Mongo: drop the verbose `details` field.
        details_str = gate_result.get("details") if isinstance(gate_result, dict) else None
        if isinstance(details_str, str) and details_str:
            ci_results_details_by_gate[gate_result.get("gate", f"gate_{len(ci_results)-1}")] = details_str
        slim_results = [{k: v for k, v in r.items() if k != "details"} for r in ci_results]
        slim_results = [{**r, "details_in_r2": True} if r.get("gate") in ci_results_details_by_gate else r for r in slim_results]

        await col.update_one(
            {"_id": oid},
            {"$set": {"ci_results": slim_results, "ci_results_r2_offloaded": True, "updated_at": datetime.utcnow()}},
        )

        # Best-effort R2 sync: writes the latest accumulated details blob.
        # Failure is logged but never blocks the pipeline — the gate result
        # is still durable in Mongo (just without `details`).
        try:
            blob = await r2_service.get_sync_request_blob(tenant_id, sync_request_id) or {}
            blob.setdefault("files", [])  # preserve files written at raise time
            blob["ci_results_details"] = ci_results_details_by_gate
            await r2_service.save_sync_request_blob(
                tenant_id=tenant_id, sync_request_id=sync_request_id, payload=blob,
            )
        except Exception as _exc:
            logger.warning("sync_request.gate_details_r2_failed",
                           sync_request_id=sync_request_id,
                           gate=gate_result.get("gate"), error=str(_exc))

        _broadcast(tenant_id, "sync:ci_progress", {
            "sync_request_id": sync_request_id,
            "gate": gate_result,   # broadcast the FULL gate result via SSE
        })

    # ── Gate: GitHub Sync — push connector files to GitHub (for PR creation later) ─
    # The branch is NOT used for security scanning (that now runs locally); it is
    # stored so raise_pr_for_sync_request can open the PR without re-uploading.
    # This is a first-class CI gate: if the push fails (bad/expired token, repo
    # access, network), the whole sync request FAILS with a clear reason instead of
    # silently reporting ci_passed while nothing reached GitHub.
    _broadcast(tenant_id, "sync:ci_progress", {
        "sync_request_id": sync_request_id,
        "gate": {"gate": "github_sync", "status": "running", "summary": "Pushing branch to GitHub..."},
    })
    branch_commit_sha: Optional[str] = None
    if not (branch_name and sync_settings and sync_settings.get("github_repo_url") and sync_settings.get("github_token")):
        await _update_gate({
            "gate": "github_sync", "status": "failed",
            "summary": "GitHub not configured",
            "details": "GitHub repo URL and token must be configured in Sync Settings before the branch can be pushed.",
        })
        return ci_results, files
    try:
        owner, repo = _parse_repo(sync_settings["github_repo_url"])
        token = sync_settings["github_token"]
        sr_doc = await col.find_one({"_id": oid}, {"target_branch": 1})
        target_branch = (sr_doc or {}).get("target_branch", "connector-development")
        branch_commit_sha = await _push_branch(
            owner, repo, token,
            branch_name, target_branch,
            files,
            commit_message=f"[Shielva CI] {branch_name}",
        )
        await col.update_one(
            {"_id": oid},
            {"$set": {"branch_commit_sha": branch_commit_sha, "updated_at": datetime.utcnow()}},
        )
        logger.info("ci_branch_pushed", sync_request_id=sync_request_id, branch=branch_name, sha=branch_commit_sha[:8])
        await _update_gate({
            "gate": "github_sync", "status": "passed",
            "summary": f"Branch pushed to GitHub ({branch_commit_sha[:8]})",
        })
    except Exception as push_err:
        logger.warning("ci_branch_push_failed", error=str(push_err)[:300], sync_request_id=sync_request_id)
        await _update_gate({
            "gate": "github_sync", "status": "failed",
            "summary": "GitHub push failed",
            "details": str(push_err)[:1000],
        })
        # Abort the pipeline — a sync request that can't reach GitHub is a failure.
        return ci_results, files

    # Gate 1: Security Audit — full scan via SDK (local files) or regex fallback
    _broadcast(tenant_id, "sync:ci_progress", {
        "sync_request_id": sync_request_id,
        "gate": {
            "gate": "security_audit",
            "status": "running",
            "summary": (
                "Running full security scan (SAST + SCA + Secrets)..."
                if settings.SHIELVA_SECURITY_API_KEY else
                "Scanning for secrets and dangerous code..."
            ),
        },
    })

    security_result: Optional[Dict] = None
    if settings.SHIELVA_SECURITY_API_KEY:
        security_result = await _security_audit_sdk(
            sync_request_id=sync_request_id,
            branch_name=branch_name,
            commit_sha=branch_commit_sha or "",
            sync_settings=sync_settings,
            files=files,
        )

    if security_result is None:
        # Fallback: local regex scanner
        security_result = await _security_audit(files)

    await _update_gate(security_result)
    if security_result["status"] == "failed" and not run_all:
        return ci_results, files

    # Gate 2: Smart Diff
    _broadcast(tenant_id, "sync:ci_progress", {
        "sync_request_id": sync_request_id,
        "gate": {"gate": "smart_diff", "status": "running", "summary": "Filtering unnecessary files..."},
    })
    cleaned_files, diff_result = _smart_diff(files)
    await _update_gate(diff_result)
    if not cleaned_files:
        diff_result["status"] = "failed"
        diff_result["summary"] = "No meaningful files after filtering"
        await col.update_one({"_id": oid}, {"$set": {"ci_results": ci_results}})
        if not run_all:
            return ci_results, cleaned_files
        cleaned_files = files  # fall back to all files so remaining gates can still run

    # Gate 3: Import/Compilation Check — direct call (same process, no HTTP)
    _broadcast(tenant_id, "sync:ci_progress", {
        "sync_request_id": sync_request_id,
        "gate": {"gate": "import_check", "status": "running", "summary": "Checking imports and compilation..."},
    })
    import_result = {"gate": "import_check", "status": "passed", "summary": "Imports OK"}
    _import_tmpdir: Optional[Path] = None
    try:
        # Write the files being synced to a temp dir — never touches the server's
        # GENERATED_CODE_DIR or any hardcoded path; each CI run is fully isolated.
        _import_tmpdir = _write_files_to_tempdir(cleaned_files or files)
        out_dir = _import_tmpdir

        import sys as _sys
        # Connectors import the platform-provided `shared.base_connector` package,
        # which lives at the connectors repo root (sibling of generated_connectors/),
        # NOT in the synced connector files. The isolated import check must put that
        # repo root on the path so it resolves the SAME base class the connector uses
        # at runtime — otherwise every connector fails with
        # "No module named 'shared.base_connector'" even though it runs fine.
        _shared_parent = Path(settings.GENERATED_CODE_DIR).resolve().parent
        if not (_shared_parent / "shared" / "base_connector.py").exists():
            _alt = Path(__file__).resolve().parents[2]
            if (_alt / "shared" / "base_connector.py").exists():
                _shared_parent = _alt
        # out_dir (the temp dir) stays first so the connector's own modules always
        # take precedence over any same-named module at the repo root.
        pythonpath = os.pathsep.join([str(out_dir), str(_shared_parent)])

        check_script = (
            "import sys, pathlib, py_compile, ast, subprocess, traceback\n"
            "sys.path.insert(0, '.')\n"
            "cwd = pathlib.Path('.')\n"
            "py_files = sorted(\n"
            "    f for f in cwd.rglob('*.py')\n"
            "    if '__pycache__' not in f.parts and 'tests' not in f.parts\n"
            "    and not f.name.startswith('test_')\n"
            ")\n"
            "errors = []\n"
            "for py_file in py_files:\n"
            "    try:\n"
            "        py_compile.compile(str(py_file), doraise=True)\n"
            "    except py_compile.PyCompileError as e:\n"
            "        errors.append(f'SyntaxError in {py_file.name}: {e}')\n"
            "TYPING_NAMES = {'Optional','List','Dict','Tuple','Union','Set','Any','Callable','Type','Sequence','Iterable','Generator','Iterator','ClassVar','Final','Literal','TypeVar','overload'}\n"
            "for py_file in py_files:\n"
            "    try:\n"
            "        src = py_file.read_text(encoding='utf-8')\n"
            "        tree = ast.parse(src)\n"
            "    except SyntaxError:\n"
            "        continue\n"
            "    imported = set()\n"
            "    for node in ast.walk(tree):\n"
            "        if isinstance(node, ast.Import):\n"
            "            for a in node.names: imported.add(a.asname or a.name.split('.')[0])\n"
            "        elif isinstance(node, ast.ImportFrom):\n"
            "            for a in node.names: imported.add(a.asname or a.name)\n"
            "    ann_names = set()\n"
            "    for node in ast.walk(tree):\n"
            "        if isinstance(node, ast.AnnAssign) and node.annotation:\n"
            "            ann_names.update(n.id for n in ast.walk(node.annotation) if isinstance(n, ast.Name))\n"
            "        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):\n"
            "            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:\n"
            "                if arg.annotation: ann_names.update(n.id for n in ast.walk(arg.annotation) if isinstance(n, ast.Name))\n"
            "            if node.returns: ann_names.update(n.id for n in ast.walk(node.returns) if isinstance(n, ast.Name))\n"
            "    missing = (ann_names & TYPING_NAMES) - imported\n"
            "    if missing:\n"
            "        errors.append(f'Missing typing imports in {py_file.name}: add: from typing import {\", \".join(sorted(missing))}')\n"
            # Security gate: mirror the gateway's AST scanner so banned calls fail the
            # BUILD (with a clear message) instead of silently passing to install.
            # Scans ALL .py files including tests (the gateway scans the whole package).
            "BLOCKED_CALLS = {'eval', 'exec', 'compile', '__import__'}\n"
            "for py_file in sorted(f for f in cwd.rglob('*.py') if '__pycache__' not in f.parts):\n"
            "    try:\n"
            "        btree = ast.parse(py_file.read_text(encoding='utf-8'))\n"
            "    except SyntaxError:\n"
            "        continue\n"
            "    for bnode in ast.walk(btree):\n"
            "        if isinstance(bnode, ast.Call) and isinstance(bnode.func, ast.Name) and bnode.func.id in BLOCKED_CALLS:\n"
            "            errors.append(f'Security: {py_file.name} calls {bnode.func.id}() - banned; the gateway AST scanner will refuse to load this connector. Use a normal top-level import instead.')\n"
            "if not errors:\n"
            "    top_mods = sorted(f.stem for f in cwd.glob('*.py')\n"
            "                      if f.stem != '__init__' and not f.stem.startswith('test_'))\n"
            "    for mod_name in top_mods:\n"
            "        try:\n"
            "            r = subprocess.run(\n"
            "                [sys.executable, '-c', f'import sys; sys.path.insert(0,\".\"); import {mod_name}'],\n"
            "                cwd=str(cwd), capture_output=True, text=True, timeout=5,\n"
            "                env=__import__(\"os\").environ.copy(),\n"
            "            )\n"
            "            if r.returncode != 0:\n"
            "                err = (r.stdout + r.stderr).strip()\n"
            "                last = [l for l in err.split('\\n') if l.strip() and not l.startswith(' ')][-1] if err else 'unknown error'\n"
            "                errors.append(f'ImportError in {mod_name}: {last}')\n"
            "        except subprocess.TimeoutExpired:\n"
            "            pass\n"
            "        except Exception as e:\n"
            "            errors.append(f'ImportError in {mod_name}: {e}')\n"
            "if errors:\n"
            "    print('COMPILE ERRORS FOUND:'); [print(e) for e in errors]\n"
            "else:\n"
            "    print('OK: all files compile clean')\n"
        )

        # Use create_subprocess_exec so CancelledError actually kills the process.
        _import_proc = await asyncio.create_subprocess_exec(
            _sys.executable, "-c", check_script,
            cwd=str(out_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": pythonpath},
        )
        try:
            _imp_stdout, _imp_stderr = await asyncio.wait_for(
                _import_proc.communicate(), timeout=30
            )
        except (asyncio.TimeoutError, asyncio.CancelledError) as _imp_exc:
            try:
                _import_proc.kill()
                await asyncio.wait_for(_import_proc.wait(), timeout=5)
            except Exception:
                pass
            raise _imp_exc
        output = (
            _imp_stdout.decode(errors="replace") + _imp_stderr.decode(errors="replace")
        ).strip() or "OK: all files compile clean"
        clean = output.startswith("OK:")
        if not clean:
            import_result["status"] = "failed"
            import_result["summary"] = "Import errors detected"
            import_result["details"] = output
    except Exception as e:
        import_result["status"] = "failed"
        import_result["summary"] = f"Import check error: {str(e)[:120]}"
    finally:
        if _import_tmpdir and _import_tmpdir.exists():
            shutil.rmtree(_import_tmpdir, ignore_errors=True)
    await _update_gate(import_result)
    if import_result["status"] == "failed" and not run_all:
        return ci_results, cleaned_files

    # Gates 4 & 5: write sync request files to a shared temp dir so run_tests
    # operates on the exact files being synced — not whatever is on the server's disk.
    _tests_tmpdir: Optional[Path] = None
    try:
        _tests_tmpdir = _write_files_to_tempdir(cleaned_files or files)

        # Gate 4: Unit Tests
        _broadcast(tenant_id, "sync:ci_progress", {
            "sync_request_id": sync_request_id,
            "gate": {"gate": "unit_tests", "status": "running", "summary": "Running unit tests..."},
        })
        unit_result = {"gate": "unit_tests", "status": "passed", "summary": "Unit tests passed"}
        try:
            pytest_data = await _run_pytest_cancellable(_tests_tmpdir, test_mode="unit")
            passed = pytest_data.get("passed", 0)
            failed = pytest_data.get("failed", 0)
            errors = pytest_data.get("errors", 0)
            unit_result["summary"] = f"{passed} passed, {failed} failed, {errors} errors"
            if failed > 0 or errors > 0:
                unit_result["status"] = "failed"
                unit_result["details"] = json.dumps(pytest_data.get("details", ""))
        except asyncio.CancelledError:
            raise  # propagate — _run_pipeline's except will handle cleanup
        except Exception as e:
            unit_result["status"] = "failed"
            unit_result["summary"] = f"Unit tests error: {str(e)[:120]}"
        await _update_gate(unit_result)
        if unit_result["status"] == "failed" and not run_all:
            return ci_results, cleaned_files

        # Gate 5: Integration Tests — always run when run_all=True, else only if test_type == "both"
        if run_all or test_type == "both":
            _broadcast(tenant_id, "sync:ci_progress", {
                "sync_request_id": sync_request_id,
                "gate": {"gate": "integration_tests", "status": "running", "summary": "Running integration tests..."},
            })
            int_result = {"gate": "integration_tests", "status": "passed", "summary": "Integration tests passed"}
            try:
                pytest_data = await _run_pytest_cancellable(_tests_tmpdir, test_mode="full")
                passed = pytest_data.get("passed", 0)
                failed = pytest_data.get("failed", 0)
                errors = pytest_data.get("errors", 0)
                int_result["summary"] = f"{passed} passed, {failed} failed, {errors} errors"
                if failed > 0 or errors > 0:
                    int_result["status"] = "failed"
                    int_result["details"] = json.dumps(pytest_data.get("details", ""))
            except asyncio.CancelledError:
                raise  # propagate — _run_pipeline's except will handle cleanup
            except Exception as e:
                int_result["status"] = "failed"
                int_result["summary"] = f"Integration tests error: {str(e)[:120]}"
            await _update_gate(int_result)
        else:
            int_result = {
                "gate": "integration_tests",
                "status": "skipped",
                "summary": "Skipped — session configured for unit tests only",
            }
            await _update_gate(int_result)
    finally:
        if _tests_tmpdir and _tests_tmpdir.exists():
            shutil.rmtree(_tests_tmpdir, ignore_errors=True)

    return ci_results, cleaned_files


# ══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@sync_request_router.post("/raise")
async def raise_sync_request(
    body: RaiseSyncRequestBody,
    request: Request,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Raise a new sync request — triggers CI pipeline, then creates PR if all gates pass."""
    user_role = (x_user_role or "viewer").lower()
    user_email = x_user_email or "unknown"

    # Payload size check
    content_length = int(request.headers.get("content-length", 0))
    if content_length > MAX_SYNC_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Payload too large ({content_length} bytes, max {MAX_SYNC_PAYLOAD_BYTES})",
        )

    # Permission check
    perms = SYNC_PERMISSIONS.get(user_role, SYNC_PERMISSIONS["viewer"])
    if not perms["can_raise"]:
        raise HTTPException(status_code=403, detail=f"Role '{user_role}' cannot raise sync requests")

    # Branch access check
    allowed_branches = BRANCH_ACCESS.get(user_role, [])
    if body.target_branch not in allowed_branches:
        raise HTTPException(
            status_code=403,
            detail=f"Role '{user_role}' cannot target branch '{body.target_branch}'. Allowed: {allowed_branches}",
        )

    # Path traversal check on all files
    for f in body.files:
        if not _validate_path(f.path):
            raise HTTPException(status_code=400, detail=f"Invalid file path: {f.path}")

    # Enforce the tenant segment from the AUTHENTICATED tenant — never client input.
    # The desktop app builds `generated_connectors/{tenantId}/...` from a LOCAL setting
    # that can be a placeholder (e.g. "shielva-sense"), but the gateway + connector loader
    # key on the JWT-derived tenant ({x_tenant_id}). Without this rewrite the PR lands at
    # generated_connectors/<placeholder>/... and the loader never picks it up under the
    # real tenant. (Also a CC6.7 tenant-isolation fix: path derives from auth, not client.)
    import re as _re_tenant
    _gc_prefix = _re_tenant.compile(r'^generated_connectors/[^/]+/')
    for f in body.files:
        if f.path.startswith("generated_connectors/") and _gc_prefix.match(f.path):
            f.path = _gc_prefix.sub(f"generated_connectors/{x_tenant_id}/", f.path)

    col = _sync_requests_col()

    # Atomic hold check — use find_one with conditions to avoid race window
    # Check if ANY active sync request in this tenant has a hold
    held = await col.find_one({
        "tenant_id": x_tenant_id,
        "held_by": {"$ne": None},
        "status": {"$nin": ["merged", "dismissed", "error"]},
    })
    if held:
        raise HTTPException(
            status_code=409,
            detail=f"Sync queue is held by {held.get('held_by', 'unknown')}. Release the hold before raising a new request.",
        )

    # Get sync settings
    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    if not sync_settings.get("github_repo_url") or not sync_settings.get("github_token"):
        raise HTTPException(status_code=400, detail="GitHub repo URL and token must be configured in sync settings before raising a request.")

    # Create sync request document
    now = datetime.utcnow()
    ts = int(time.time())
    sanitized_user = re.sub(r"[^a-z0-9_]", "_", user_email.split("@")[0].lower())
    dt_str = now.strftime("%Y%m%d_%H%M%S")
    checksum_input = f"{body.session_id}:{body.connector_name}:{ts}"
    short_hash = hashlib.sha256(checksum_input.encode()).hexdigest()[:8]
    branch_name = f"SRQ_{sanitized_user}_{dt_str}_{short_hash}"
    # Phase 6: PR file contents go to R2 (compressed). Mongo only keeps
    # `file_count` + `files_r2_offloaded:True` so the list endpoint stays tiny.
    # The detail/diff endpoints lazy-fetch from R2 in a single call.
    _files_payload = [{"path": f.path, "content": f.content} for f in body.files]
    doc = {
        "tenant_id": x_tenant_id,
        "session_id": body.session_id,
        "connector_name": body.connector_name,
        "target_branch": body.target_branch,
        "branch_name": branch_name,
        "status": "validating",
        "raised_by": user_email,
        "ci_results": [],
        "pr_number": None,
        "pr_url": None,
        "diff": None,
        "held_by": None,
        "held_at": None,
        "approved_by": None,
        "merged_at": None,
        "error": None,
        "file_count": len(_files_payload),
        "files_r2_offloaded": True,
        "created_at": now,
        "updated_at": now,
    }
    result = await col.insert_one(doc)
    sync_request_id = str(result.inserted_id)
    # Upload files to R2 keyed by the freshly-allocated sync_request_id.
    # Best-effort: if R2 is briefly unavailable the row still exists in
    # Mongo and the CI pipeline that needs files can retry — better than
    # losing the request entirely on a transient cloud blip.
    try:
        await r2_service.save_sync_request_blob(
            tenant_id=x_tenant_id,
            sync_request_id=sync_request_id,
            payload={"files": _files_payload, "ci_results_details": {}},
        )
    except Exception as _exc:
        logger.warning("sync_request.r2_files_save_failed",
                       sync_request_id=sync_request_id, error=str(_exc))

    _broadcast(x_tenant_id, "sync:request_created", {
        "sync_request_id": sync_request_id,
        "connector_name": body.connector_name,
        "raised_by": user_email,
        "status": "validating",
    })

    # Run CI pipeline in background (semaphore caps concurrent GitHub API usage
    # to avoid hitting GitHub's secondary rate limit when many requests run together)
    async def _run_pipeline():
        async with _CI_SEMAPHORE:
            try:
                ci_results, cleaned_files = await _run_ci_pipeline(
                    sync_request_id, x_tenant_id, body.session_id, body.files,
                    branch_name=branch_name,
                    sync_settings=sync_settings,
                )

                # Check if any gate failed
                any_failed = any(r["status"] == "failed" for r in ci_results)
                if any_failed:
                    await _delete_ci_branch(branch_name, sync_settings)
                    await col.update_one(
                        {"_id": ObjectId(sync_request_id)},
                        {"$set": {"status": "validation_failed", "branch_commit_sha": None, "updated_at": datetime.utcnow()}},
                    )
                    _broadcast(x_tenant_id, "sync:request_validation_failed", {
                        "sync_request_id": sync_request_id,
                        "ci_results": ci_results,
                    })
                    return

                if not cleaned_files:
                    await _delete_ci_branch(branch_name, sync_settings)
                    await col.update_one(
                        {"_id": ObjectId(sync_request_id)},
                        {"$set": {"status": "validation_failed", "error": "No files to sync after filtering", "branch_commit_sha": None, "updated_at": datetime.utcnow()}},
                    )
                    return

                await col.update_one(
                    {"_id": ObjectId(sync_request_id)},
                    {"$set": {"status": "ci_passed", "updated_at": datetime.utcnow()}},
                )
                _broadcast(x_tenant_id, "sync:ci_passed", {
                    "sync_request_id": sync_request_id,
                    "ci_results": ci_results,
                })

            except asyncio.CancelledError:
                logger.info("sync_request.pipeline_cancelled", sync_request_id=sync_request_id)
                await _delete_ci_branch(branch_name, sync_settings)
                await col.update_one(
                    {"_id": ObjectId(sync_request_id)},
                    {"$set": {"status": "dismissed", "error": "CI cancelled by user", "branch_commit_sha": None, "updated_at": datetime.utcnow()}},
                )
                _broadcast(x_tenant_id, "sync:request_cancelled", {"sync_request_id": sync_request_id})

            except Exception as e:
                logger.error("sync_request.pipeline_error", error=str(e), sync_request_id=sync_request_id)
                await _delete_ci_branch(branch_name, sync_settings)
                await col.update_one(
                    {"_id": ObjectId(sync_request_id)},
                    {"$set": {"status": "error", "error": str(e)[:500], "branch_commit_sha": None, "updated_at": datetime.utcnow()}},
                )
                _broadcast(x_tenant_id, "sync:request_error", {
                    "sync_request_id": sync_request_id,
                    "error": str(e)[:200],
                })

    _task = asyncio.create_task(_run_pipeline())
    _ci_tasks[sync_request_id] = _task
    _task.add_done_callback(lambda _: _ci_tasks.pop(sync_request_id, None))

    return {
        "sync_request_id": sync_request_id,
        "status": "validating",
        "message": "Sync request created. CI pipeline is running.",
    }


@sync_request_router.get("")
async def list_sync_requests(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    status: Optional[str] = Query(None),
    include_terminal: bool = Query(False),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=1000),
):
    """List sync requests for a tenant — tuned for fast panel response.

    Pagination: panel default is the most-recent 20 rows; subsequent pages
    fetch via `?skip=N`. The detail/diff payloads (`files[]`,
    `ci_results[].details`) live in R2 and are hydrated only when the
    per-card detail endpoint is called.

    The total-count header lets the panel render "Showing 20 of N".
    """
    col = _sync_requests_col()
    query: Dict[str, Any] = {"tenant_id": x_tenant_id}
    if status:
        query["status"] = status
    elif not include_terminal:
        # Default: hide rows the panel filters out anyway. Massive win on
        # tenants that have accumulated months of merge history.
        query["status"] = {"$nin": ["merged", "dismissed", "error"]}

    # Inclusive projection: ONLY the fields the row renders + the ones the
    # client-side derivation logic needs. ci_results compressed to a count
    # + last gate name; full per-gate breakdown loads when the card opens.
    projection = {
        "_id": 1, "tenant_id": 1, "session_id": 1, "connector_name": 1,
        "status": 1, "pr_state": 1,
        "target_branch": 1, "branch_name": 1,
        "pr_number": 1, "pr_url": 1,
        "raised_by": 1, "approved_by": 1,
        "created_at": 1, "updated_at": 1, "merged_at": 1,
        "error": 1,
        # ci_results compressed: per-gate {gate, status, summary} only,
        # never the verbose `details` blob.
        "ci_results.gate": 1,
        "ci_results.status": 1,
        "ci_results.summary": 1,
    }
    total = await col.count_documents(query)
    cursor = col.find(query, projection).sort("created_at", -1).skip(skip).limit(limit)
    items = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        items.append(doc)
    return {"items": items, "total": total, "skip": skip, "limit": limit}


class ReconcileRequest(BaseModel):
    # When set, reconcile only this one sync request (per-card refresh icon).
    # When omitted, reconcile every non-terminal sync request for the tenant
    # (login-time bulk reconcile).
    sync_request_id: Optional[str] = None


def _parse_gh_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parse a GitHub ISO-8601 timestamp (e.g. '2026-06-14T07:57:04Z')."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


@sync_request_router.post("/reconcile")
async def reconcile_sync_requests(
    body: ReconcileRequest = ReconcileRequest(),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Reconcile sync-request PR status against GitHub (pull-based truth).

    For each tracked sync request in a non-terminal state with a PR number,
    fetch the PR's real state from GitHub and update Mongo if it diverged —
    merged on GitHub but still ``ready`` locally, or closed-without-merge.
    Broadcasts the same SSE events the webhook does, so connected cards update live.

    This is the pull-based complement to the push-based GitHub webhook: it catches
    merges/closes that were never delivered (tunnel down, server restart, or a PR
    merged directly on GitHub). Used by the login-time bulk reconcile (no id) and
    the per-card refresh icon (with ``sync_request_id``).
    """
    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    token = sync_settings.get("github_token", "")
    repo_url = sync_settings.get("github_repo_url", "")
    if not token or not repo_url:
        raise HTTPException(status_code=400, detail="GitHub token and repo URL must be configured first.")
    owner, repo = _parse_repo(repo_url)

    col = _sync_requests_col()
    # Non-terminal statuses whose PR can still change on GitHub.
    open_states = ["validating", "ci_passed", "ready", "approving"]
    query: Dict[str, Any] = {"tenant_id": x_tenant_id, "pr_number": {"$ne": None}}
    if body.sync_request_id:
        try:
            query["_id"] = ObjectId(body.sync_request_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid sync request ID")
    else:
        query["status"] = {"$in": open_states}

    reconciled: List[Dict[str, Any]] = []
    async for doc in col.find(query):
        pr_number = doc.get("pr_number")
        if not pr_number:
            continue
        try:
            pr = await _github_request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}", token)
        except HTTPException as exc:
            logger.warning("reconcile.pr_fetch_failed", pr_number=pr_number, status=exc.status_code)
            continue

        gh_merged = bool(pr.get("merged", False))
        gh_state = pr.get("state")  # "open" | "closed"
        local_status = doc.get("status")
        sid = str(doc["_id"])
        now = datetime.utcnow()

        # Live PR lifecycle state, ALWAYS persisted (even for terminal local statuses)
        # so the UI can show "PR closed"/"PR merged" instead of forever "PR open".
        pr_state = "merged" if gh_merged else (gh_state or "open")  # open | closed | merged
        set_fields: Dict[str, Any] = {"pr_state": pr_state, "updated_at": now}

        if gh_merged and local_status != "merged":
            merged_at = _parse_gh_ts(pr.get("merged_at")) or now
            set_fields.update({"status": "merged", "merged_at": merged_at})
            await col.update_one({"_id": doc["_id"]}, {"$set": set_fields})
            _broadcast(x_tenant_id, "sync:request_merged", {
                "sync_request_id": sid,
                "approved_by": "github-reconcile",
                "merged_at": merged_at.isoformat(),
            })
            reconciled.append({"sync_request_id": sid, "pr_number": pr_number, "from": local_status, "to": "merged"})
        elif gh_state == "closed" and not gh_merged and local_status not in ("merged", "dismissed"):
            set_fields["status"] = "dismissed"
            await col.update_one({"_id": doc["_id"]}, {"$set": set_fields})
            _broadcast(x_tenant_id, "sync:request_dismissed", {
                "sync_request_id": sid, "reason": "closed_on_github",
            })
            reconciled.append({"sync_request_id": sid, "pr_number": pr_number, "from": local_status, "to": "dismissed"})
        else:
            # No status transition, but the PR state on GitHub may still have changed
            # (e.g. an already-dismissed request whose PR was just closed). Persist it +
            # broadcast so the open client re-renders the PR badge.
            await col.update_one({"_id": doc["_id"]}, {"$set": set_fields})
            _broadcast(x_tenant_id, "sync:pr_state", {"sync_request_id": sid, "pr_state": pr_state})
            reconciled.append({"sync_request_id": sid, "pr_number": pr_number, "from": local_status, "to": local_status, "pr_state": pr_state})

    logger.info("reconcile.done", tenant_id=x_tenant_id, reconciled=len(reconciled),
                scope=("single" if body.sync_request_id else "all"))
    return {"reconciled_count": len(reconciled), "reconciled": reconciled}


def _git_blob_sha(content: str) -> str:
    """Git blob SHA-1 for text content — matches the SHA GitHub stores for a pushed file."""
    import hashlib
    data = content.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class DiffPreviewBody(BaseModel):
    connector_name: str
    target_branch: str = "connector-development"
    files: List[SyncFilePayload]


@sync_request_router.post("/diff-preview")
async def diff_preview(
    body: DiffPreviewBody,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Whether the submitted local files differ from what's already on the target branch.

    Lets the UI HIDE "Raise Sync Request" when there's nothing to sync. Compares git blob
    SHAs against generated_connectors/{tenant}/{connector}/ — one Trees API call, no diff
    of file bodies. Safe default: when it can't determine, returns has_diff=true so the
    button stays available (never hide a real change).
    """
    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    token = sync_settings.get("github_token", "")
    repo_url = sync_settings.get("github_repo_url", "")
    if not token or not repo_url:
        return {"has_diff": True, "reason": "sync_not_configured"}
    owner, repo = _parse_repo(repo_url)

    # Normalize tenant segment + strip junk so we compare exactly what WOULD be pushed.
    import re as _re_dp
    _gc = _re_dp.compile(r'^generated_connectors/[^/]+/')
    norm: List[SyncFilePayload] = []
    for f in body.files:
        p = _gc.sub(f"generated_connectors/{x_tenant_id}/", f.path) if (f.path.startswith("generated_connectors/") and _gc.match(f.path)) else f.path
        norm.append(SyncFilePayload(path=p, content=f.content))
    cleaned, _ = _smart_diff(norm, drop_empty=False)  # keep empty files — branch has them too

    prefix = f"generated_connectors/{x_tenant_id}/{body.connector_name}".rstrip("/")
    local = {f.path: _git_blob_sha(f.content) for f in cleaned}

    repo_tree: Dict[str, str] = {}
    try:
        tree = await _github_request("GET", f"/repos/{owner}/{repo}/git/trees/{body.target_branch}?recursive=1", token)
        for item in tree.get("tree", []):
            ipath = item.get("path", "")
            if item.get("type") == "blob" and ipath.startswith(prefix + "/"):
                repo_tree[ipath] = item.get("sha", "")
    except HTTPException as exc:
        # Branch/tree/connector dir not found → connector was never synced → all new.
        logger.info("diff_preview.tree_unavailable", status=exc.status_code, prefix=prefix)
        return {"has_diff": bool(local), "added": list(local.keys()), "modified": [], "removed": []}

    added = [p for p in local if p not in repo_tree]
    removed = [p for p in repo_tree if p not in local]
    modified = [p for p in local if p in repo_tree and local[p] != repo_tree[p]]
    return {"has_diff": bool(added or removed or modified), "added": added, "modified": modified, "removed": removed}


class DiffDetailBody(BaseModel):
    connector_name: str
    target_branch: str = "connector-development"
    files: List[SyncFilePayload]


async def _fetch_branch_blob(owner: str, repo: str, sha: str, token: str) -> str:
    """Fetch a git blob's text content by SHA from GitHub."""
    blob = await _github_request("GET", f"/repos/{owner}/{repo}/git/blobs/{sha}", token)
    import base64 as _b64
    if blob.get("encoding") == "base64":
        return _b64.b64decode(blob.get("content", "")).decode("utf-8", errors="replace")
    return blob.get("content", "")


@sync_request_router.post("/diff-detail")
async def diff_detail(
    body: DiffDetailBody,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Per-file unified diff between the LOCAL connector code and the target branch.

    Powers the "View Changes" modal. Returns, for each changed file, a git-style
    unified diff plus add/del line counts. `configured` is false when GitHub sync
    isn't set up (so the UI can say so instead of implying a phantom diff).
    """
    import difflib
    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    token = sync_settings.get("github_token", "")
    repo_url = sync_settings.get("github_repo_url", "")
    if not token or not repo_url:
        return {"configured": False, "files": [], "reason": "sync_not_configured"}
    owner, repo = _parse_repo(repo_url)

    import re as _re_dd
    _gc = _re_dd.compile(r'^generated_connectors/[^/]+/')
    norm: List[SyncFilePayload] = []
    for f in body.files:
        p = _gc.sub(f"generated_connectors/{x_tenant_id}/", f.path) if (f.path.startswith("generated_connectors/") and _gc.match(f.path)) else f.path
        norm.append(SyncFilePayload(path=p, content=f.content))
    cleaned, _ = _smart_diff(norm, drop_empty=False)  # keep empty files — branch has them too

    prefix = f"generated_connectors/{x_tenant_id}/{body.connector_name}".rstrip("/")
    local_content = {f.path: f.content for f in cleaned}
    local_sha = {p: _git_blob_sha(c) for p, c in local_content.items()}

    repo_tree: Dict[str, str] = {}
    try:
        tree = await _github_request("GET", f"/repos/{owner}/{repo}/git/trees/{body.target_branch}?recursive=1", token)
        for item in tree.get("tree", []):
            ipath = item.get("path", "")
            if item.get("type") == "blob" and ipath.startswith(prefix + "/"):
                repo_tree[ipath] = item.get("sha", "")
    except HTTPException:
        # Connector never synced → everything is "added".
        repo_tree = {}

    added = [p for p in local_content if p not in repo_tree]
    removed = [p for p in repo_tree if p not in local_content]
    modified = [p for p in local_content if p in repo_tree and local_sha[p] != repo_tree[p]]

    def _unified(path: str, old: str, new: str) -> Dict[str, Any]:
        ol, nl = old.splitlines(keepends=True), new.splitlines(keepends=True)
        diff = list(difflib.unified_diff(ol, nl, fromfile=f"a/{path}", tofile=f"b/{path}"))
        adds = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        dels = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        return {"diff": "".join(diff), "additions": adds, "deletions": dels}

    out: List[Dict[str, Any]] = []
    for p in sorted(added):
        out.append({"path": p, "status": "added", **_unified(p, "", local_content[p])})
    for p in sorted(modified):
        branch_text = await _fetch_branch_blob(owner, repo, repo_tree[p], token)
        out.append({"path": p, "status": "modified", **_unified(p, branch_text, local_content[p])})
    for p in sorted(removed):
        branch_text = await _fetch_branch_blob(owner, repo, repo_tree[p], token)
        out.append({"path": p, "status": "removed", **_unified(p, branch_text, "")})

    return {"configured": True, "has_diff": bool(out), "files": out}


# ── Promote: supersede the previously-built connector with the enhanced version ──

_PROMOTE_TARGET_BRANCH = "connector-development"
# Files under the connector dir that aren't part of the connector's committed code.
_OUTDATED_SKIP = re.compile(r'(^|/)(__pycache__|\.pytest_cache|\.ruff_cache|node_modules)(/|$)|\.pyc$')


def _deployed_connector_dir(tenant_id: str, connector_name: str) -> Path:
    """On-disk dir of the currently-built (deployed) connector for this tenant."""
    return Path(settings.GENERATED_CODE_DIR) / tenant_id / connector_name


def _read_deployed_files(tenant_id: str, connector_name: str) -> Dict[str, str]:
    """Map of repo-relative path → git blob SHA for the deployed connector on disk."""
    base = _deployed_connector_dir(tenant_id, connector_name)
    out: Dict[str, str] = {}
    if not base.exists():
        return out
    for fp in base.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(base).as_posix()
        if _OUTDATED_SKIP.search(rel):
            continue
        try:
            content = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out[f"generated_connectors/{tenant_id}/{connector_name}/{rel}"] = _git_blob_sha(content)
    return out


async def _merged_sync_request(tenant_id: str, connector_name: str) -> Optional[Dict]:
    """Most recent sync request for this connector whose PR is merged to connector-development."""
    return await _sync_requests_col().find_one(
        {"tenant_id": tenant_id, "connector_name": connector_name, "status": "merged"},
        sort=[("merged_at", -1)],
    )


@sync_request_router.get("/outdated")
async def connector_outdated(
    connector_name: str = Query(...),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Whether the deployed (previously-built) connector differs from connector-development.

    `outdated` is true on ANY file difference. `promotable` is true when a merged PR
    exists for this connector — i.e. the enhanced code is on connector-development and
    can be pulled in. Drives the Outdated badge + Promote affordance and step gating.
    """
    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    token = sync_settings.get("github_token", "")
    repo_url = sync_settings.get("github_repo_url", "")
    merged = await _merged_sync_request(x_tenant_id, connector_name)
    promotable = bool(merged)
    if not token or not repo_url:
        # Can't compare → don't cry wolf. Outdated only if we have a merged PR to pull.
        return {"outdated": promotable, "promotable": promotable, "changed": [], "reason": "sync_not_configured",
                "sync_request_id": str(merged["_id"]) if merged else None}
    owner, repo = _parse_repo(repo_url)
    target_branch = sync_settings.get("default_target_branch") or _PROMOTE_TARGET_BRANCH

    local = _read_deployed_files(x_tenant_id, connector_name)
    prefix = f"generated_connectors/{x_tenant_id}/{connector_name}"
    repo_tree: Dict[str, str] = {}
    try:
        tree = await _github_request("GET", f"/repos/{owner}/{repo}/git/trees/{target_branch}?recursive=1", token)
        for item in tree.get("tree", []):
            ipath = item.get("path", "")
            if item.get("type") == "blob" and ipath.startswith(prefix + "/") and not _OUTDATED_SKIP.search(ipath):
                repo_tree[ipath] = item.get("sha", "")
    except HTTPException as exc:
        logger.info("outdated.tree_unavailable", status=exc.status_code, prefix=prefix)
        return {"outdated": False, "promotable": promotable, "changed": [],
                "sync_request_id": str(merged["_id"]) if merged else None}

    changed = (
        [p for p in repo_tree if p not in local]                                   # added on dev
        + [p for p in local if p not in repo_tree]                                  # removed on dev
        + [p for p in local if p in repo_tree and local[p] != repo_tree[p]]         # modified
    )
    return {
        "outdated": bool(changed),
        "promotable": promotable,
        "changed": changed,
        "sync_request_id": str(merged["_id"]) if merged else None,
    }


class PromoteBody(BaseModel):
    connector_name: str
    session_id: str = ""   # the enhanced session to keep as primary (others are deleted)


@sync_request_router.post("/promote")
async def promote_connector(
    body: PromoteBody,
    request: Request,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Promote the enhanced connector to primary, superseding the previous build.

    Requires the PR to be merged to connector-development. Pulls that code in-place
    (connector_type unchanged → stored credentials are preserved), reloads the
    connector, then deletes the superseded build/enhancement sessions.
    """
    merged = await _merged_sync_request(x_tenant_id, body.connector_name)
    if not merged:
        raise HTTPException(
            status_code=409,
            detail="Nothing to promote — no PR merged to connector-development for this connector yet.",
        )

    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    token = sync_settings.get("github_token", "")
    repo_url = sync_settings.get("github_repo_url", "")
    if not token or not repo_url:
        raise HTTPException(status_code=400, detail="GitHub repo URL and token must be configured to promote.")

    # ── Pull the merged code in-place + hot-reload (preserves connector_type + creds) ──
    # Authenticate to the internal reload endpoint with the shared service token
    # (NOT by spoofing a role). The caller's real role is forwarded for audit only.
    target_branch = sync_settings.get("default_target_branch") or _PROMOTE_TARGET_BRANCH
    import os as _os
    internal_token = _os.getenv("CONNECTOR_INTERNAL_TOKEN", "")
    if not internal_token:
        # Robust fallback: read straight from core/.env by EXPLICIT path (auto-discovery
        # finds core/integration/.env first, which lacks this secret).
        try:
            from dotenv import dotenv_values
            from pathlib import Path as _P
            _core_env = _P(__file__).resolve().parents[2] / ".env"   # core/.env
            internal_token = (dotenv_values(_core_env) or {}).get("CONNECTOR_INTERNAL_TOKEN", "") or ""
        except Exception:
            internal_token = ""
    logger.info("promote.reload_call", token_present=bool(internal_token), token_len=len(internal_token), gateway=settings.CONNECTOR_GATEWAY_URL)
    try:
        async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
            resp = await client.post(
                f"{settings.CONNECTOR_GATEWAY_URL}/internal/pull-and-reload",
                headers={
                    "X-Tenant-ID": x_tenant_id,
                    "X-Internal-Token": internal_token,
                    "X-User-Role": (request.headers.get("X-User-Role") or ""),
                },
                json={"target_branch": target_branch, "github_token": token, "github_repo_url": repo_url},
            )
            resp.raise_for_status()
            reload_result = resp.json()
    except httpx.HTTPError as e:
        logger.error("promote.pull_and_reload_failed", connector=body.connector_name, error=str(e))
        raise HTTPException(status_code=502, detail=f"Failed to pull/reload promoted code: {e}")

    # ── Delete superseded sessions: every session for this connector EXCEPT the kept one ──
    # Safety: a kept session is REQUIRED. Without a valid session_id we must never
    # mass-delete (that would wipe every session for the connector). Refuse instead.
    from integration.db.database import sessions_collection
    try:
        keep_oid = ObjectId(body.session_id) if body.session_id else None
    except Exception:
        keep_oid = None
    if keep_oid is None:
        raise HTTPException(
            status_code=400,
            detail="promote requires a valid session_id to keep — refusing to delete all sessions.",
        )
    del_res = await sessions_collection().delete_many(
        {"tenant_id": x_tenant_id, "connector_name": body.connector_name, "_id": {"$ne": keep_oid}}
    )

    _broadcast(x_tenant_id, "sync:connector_promoted", {
        "connector_name": body.connector_name,
        "kept_session_id": body.session_id or None,
        "deleted_sessions": del_res.deleted_count,
    })
    logger.info("promote.success", connector=body.connector_name, tenant=x_tenant_id,
                deleted_sessions=del_res.deleted_count, reload=reload_result.get("reload"))
    return {
        "promoted": True,
        "connector_name": body.connector_name,
        "deleted_sessions": del_res.deleted_count,
        "reload": reload_result.get("reload"),
    }


@sync_request_router.get("/events")
async def sync_events_stream(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """SSE stream — pushes real-time sync updates to all connected clients for a tenant."""
    q: asyncio.Queue = asyncio.Queue(maxsize=100)

    if x_tenant_id not in _sse_clients:
        _sse_clients[x_tenant_id] = set()
    _sse_clients[x_tenant_id].add(q)

    async def _stream() -> AsyncIterator[str]:
        try:
            # Initial ping
            yield f"event: connected\ndata: {json.dumps({'tenant_id': x_tenant_id})}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield msg
                except asyncio.TimeoutError:
                    # Keep-alive ping
                    yield f"event: ping\ndata: {json.dumps({'ts': int(time.time() * 1000)})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _sse_clients.get(x_tenant_id, set()).discard(q)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@sync_request_router.get("/counts")
async def sync_request_counts(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Per-status counts for the tenant — single aggregate, no payload.

    Defined BEFORE the dynamic ``/{sync_request_id}`` route so FastAPI
    matches the literal ``/counts`` path instead of treating it as an ID.
    """
    col = _sync_requests_col()
    pipeline = [
        {"$match": {"tenant_id": x_tenant_id}},
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
    ]
    by_status: Dict[str, int] = {}
    async for d in col.aggregate(pipeline):
        by_status[d["_id"] or "unknown"] = d["n"]
    return {
        "by_status": by_status,
        "mergeable": by_status.get("ci_passed", 0) + by_status.get("ready", 0),
        "active": sum(n for s, n in by_status.items()
                       if s not in ("merged", "dismissed", "error")),
        "total": sum(by_status.values()),
    }


@sync_request_router.post("/cleanup-stuck")
async def cleanup_stuck_sync_requests(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    older_than_minutes: int = Query(5, ge=1),
):
    """Roll back rows stuck mid-transition (e.g. ``approving`` after a failed
    merge). Bounded so an in-flight legitimate merge isn't disturbed."""
    if (x_user_role or "viewer").lower() != "super_admin":
        raise HTTPException(status_code=403, detail="Only super_admin can run cleanup")
    cutoff = datetime.utcnow() - timedelta(minutes=older_than_minutes)
    col = _sync_requests_col()
    r = await col.update_many(
        {"tenant_id": x_tenant_id, "status": "approving", "updated_at": {"$lt": cutoff}},
        {"$set": {"status": "ready", "updated_at": datetime.utcnow()}},
    )
    return {"rolled_back": r.modified_count}


@sync_request_router.get("/{sync_request_id}")
async def get_sync_request(
    sync_request_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Get a single sync request with full details — one call hydrates from R2.

    Mongo holds the slim metadata; R2 holds the heavy blobs (`files[]` and
    per-gate `ci_results[].details`). This handler reads both in parallel and
    stitches them back into the legacy shape so existing UI code doesn't
    need to change — the diff modal still sees `files` and per-gate
    `details` exactly where it used to.
    """
    col = _sync_requests_col()
    try:
        doc = await col.find_one({"_id": ObjectId(sync_request_id), "tenant_id": x_tenant_id})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid sync request ID")
    if not doc:
        raise HTTPException(status_code=404, detail="Sync request not found")
    doc["_id"] = str(doc["_id"])

    # Hydrate from R2 only when needed (legacy non-offloaded rows pass through).
    needs_hydration = doc.get("files_r2_offloaded") or doc.get("ci_results_r2_offloaded")
    if needs_hydration:
        try:
            blob = await r2_service.get_sync_request_blob(x_tenant_id, sync_request_id)
        except Exception as _exc:
            logger.warning("sync_request.r2_hydrate_failed",
                           sync_request_id=sync_request_id, error=str(_exc))
            blob = None
        if isinstance(blob, dict):
            if doc.get("files_r2_offloaded") and isinstance(blob.get("files"), list):
                doc["files"] = blob["files"]
            details_map = blob.get("ci_results_details") or {}
            if doc.get("ci_results_r2_offloaded") and isinstance(details_map, dict):
                merged = []
                for r in (doc.get("ci_results") or []):
                    if not isinstance(r, dict):
                        merged.append(r); continue
                    gate = r.get("gate")
                    if gate and gate in details_map and r.get("details_in_r2"):
                        r = {**r, "details": details_map[gate]}
                        r.pop("details_in_r2", None)
                    merged.append(r)
                doc["ci_results"] = merged
    # Strip internal offload markers from the client response.
    doc.pop("files_r2_offloaded", None)
    doc.pop("ci_results_r2_offloaded", None)
    return doc


@sync_request_router.post("/{sync_request_id}/rerun")
async def rerun_sync_request(
    sync_request_id: str,
    body: RerunSyncRequestBody = RerunSyncRequestBody(),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Re-run the CI pipeline for an existing sync request.

    Allowed when status is validation_failed or error.
    Resets status to validating, clears ci_results, and re-runs the pipeline
    using the files that were originally submitted (stored on the branch).
    """
    user_role = (x_user_role or "viewer").lower()
    perms = SYNC_PERMISSIONS.get(user_role, SYNC_PERMISSIONS["viewer"])
    if not perms["can_raise"]:
        raise HTTPException(status_code=403, detail=f"Role '{user_role}' cannot re-run CI")

    col = _sync_requests_col()
    try:
        doc = await col.find_one({"_id": ObjectId(sync_request_id), "tenant_id": x_tenant_id})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid sync request ID")
    if not doc:
        raise HTTPException(status_code=404, detail="Sync request not found")
    if doc["status"] not in ("validation_failed", "error", "ready", "ci_passed"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot re-run — status is '{doc['status']}'. Only validation_failed, error, ci_passed, or ready requests can be re-run.",
        )

    sync_settings = await _get_tenant_sync_settings(x_tenant_id)

    # Reset state — also clear branch_commit_sha so the branch is re-pushed
    now = datetime.utcnow()
    await col.update_one(
        {"_id": ObjectId(doc["_id"])},
        {"$set": {"status": "validating", "ci_results": [], "error": None, "branch_commit_sha": None, "updated_at": now}},
    )
    _broadcast(x_tenant_id, "sync:ci_rerun", {
        "sync_request_id": sync_request_id,
        "triggered_by": x_user_email or "unknown",
    })

    # Re-run pipeline in background using the original files stored in doc
    files = [SyncFilePayload(path=f["path"], content=f["content"]) for f in (doc.get("files") or [])]

    _rerun_branch_name = doc.get("branch_name", "")

    async def _rerun():
        async with _CI_SEMAPHORE:
            try:
                ci_results, cleaned_files = await _run_ci_pipeline(
                    sync_request_id, x_tenant_id, doc["session_id"], files, run_all=body.run_all,
                    branch_name=_rerun_branch_name,
                    sync_settings=sync_settings,
                )
                any_failed = any(r["status"] == "failed" for r in ci_results)
                if any_failed:
                    await _delete_ci_branch(_rerun_branch_name, sync_settings)
                    await col.update_one(
                        {"_id": ObjectId(sync_request_id)},
                        {"$set": {"status": "validation_failed", "branch_commit_sha": None, "updated_at": datetime.utcnow()}},
                    )
                    _broadcast(x_tenant_id, "sync:request_validation_failed", {
                        "sync_request_id": sync_request_id,
                        "ci_results": ci_results,
                    })
                    return
                if not cleaned_files:
                    await _delete_ci_branch(_rerun_branch_name, sync_settings)
                    await col.update_one(
                        {"_id": ObjectId(sync_request_id)},
                        {"$set": {"status": "validation_failed", "error": "No files to sync after filtering", "branch_commit_sha": None, "updated_at": datetime.utcnow()}},
                    )
                    return
                await col.update_one(
                    {"_id": ObjectId(sync_request_id)},
                    {"$set": {"status": "ci_passed", "pr_number": None, "pr_url": None, "diff": None, "updated_at": datetime.utcnow()}},
                )
                _broadcast(x_tenant_id, "sync:ci_passed", {
                    "sync_request_id": sync_request_id,
                    "ci_results": ci_results,
                })
            except asyncio.CancelledError:
                logger.info("sync_request.rerun_cancelled", sync_request_id=sync_request_id)
                await _delete_ci_branch(_rerun_branch_name, sync_settings)
                await col.update_one(
                    {"_id": ObjectId(sync_request_id)},
                    {"$set": {"status": "dismissed", "error": "CI cancelled by user", "branch_commit_sha": None, "updated_at": datetime.utcnow()}},
                )
                _broadcast(x_tenant_id, "sync:request_cancelled", {"sync_request_id": sync_request_id})
            except Exception as e:
                logger.error("sync_request.rerun_error", error=str(e), sync_request_id=sync_request_id)
                await _delete_ci_branch(_rerun_branch_name, sync_settings)
                await col.update_one(
                    {"_id": ObjectId(sync_request_id)},
                    {"$set": {"status": "error", "error": str(e)[:500], "branch_commit_sha": None, "updated_at": datetime.utcnow()}},
                )
                _broadcast(x_tenant_id, "sync:request_error", {"sync_request_id": sync_request_id, "error": str(e)[:200]})

    _rerun_task = asyncio.create_task(_rerun())
    _ci_tasks[sync_request_id] = _rerun_task
    _rerun_task.add_done_callback(lambda _: _ci_tasks.pop(sync_request_id, None))
    return {"status": "validating", "message": "CI pipeline re-started."}


@sync_request_router.post("/{sync_request_id}/cancel-ci")
async def cancel_ci_pipeline(
    sync_request_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Cancel a running CI pipeline immediately.

    Cancels the asyncio task (stops any in-progress gate including long-running
    SDK security scans), deletes the CI branch from GitHub, and sets status to
    'dismissed'. Can be called at any point while status is 'validating'.
    """
    user_role = (x_user_role or "viewer").lower()
    perms = SYNC_PERMISSIONS.get(user_role, SYNC_PERMISSIONS["viewer"])
    if not perms["can_raise"]:
        raise HTTPException(status_code=403, detail=f"Role '{user_role}' cannot cancel CI runs")

    col = _sync_requests_col()
    try:
        doc = await col.find_one({"_id": ObjectId(sync_request_id), "tenant_id": x_tenant_id})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid sync request ID")
    if not doc:
        raise HTTPException(status_code=404, detail="Sync request not found")
    if doc["status"] != "validating":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel — status is '{doc['status']}', expected 'validating'",
        )

    task = _ci_tasks.get(sync_request_id)
    if task and not task.done():
        # Cancel the asyncio task — raises CancelledError at the next await point
        # in _run_pipeline / _rerun, which handles cleanup (branch delete + status update)
        task.cancel()
        return {"status": "dismissed", "message": "CI pipeline cancelled."}

    # Task already finished (race condition) — the pipeline already updated status.
    # If it somehow still shows 'validating' (shouldn't happen), force-dismiss it.
    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    branch_name = doc.get("branch_name", "")
    await _delete_ci_branch(branch_name, sync_settings)
    await col.update_one(
        {"_id": ObjectId(sync_request_id)},
        {"$set": {"status": "dismissed", "error": "CI cancelled by user", "branch_commit_sha": None, "updated_at": datetime.utcnow()}},
    )
    _broadcast(x_tenant_id, "sync:request_cancelled", {"sync_request_id": sync_request_id})
    return {"status": "dismissed", "message": "CI pipeline cancelled."}


@sync_request_router.post("/{sync_request_id}/raise-pr")
async def raise_pr_for_sync_request(
    sync_request_id: str,
    body: RaisePrBody = None,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Create a GitHub PR for a sync request whose CI has passed.

    Only allowed when status is 'ci_passed'. User must explicitly call this
    after reviewing the files — PR is never created automatically.
    """
    user_role = (x_user_role or "viewer").lower()
    perms = SYNC_PERMISSIONS.get(user_role, SYNC_PERMISSIONS["viewer"])
    if not perms["can_raise"]:
        raise HTTPException(status_code=403, detail=f"Role '{user_role}' cannot raise PRs")

    col = _sync_requests_col()
    try:
        doc = await col.find_one({"_id": ObjectId(sync_request_id), "tenant_id": x_tenant_id})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid sync request ID")
    if not doc:
        raise HTTPException(status_code=404, detail="Sync request not found")
    if doc["status"] != "ci_passed":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot raise PR — status is '{doc['status']}', expected 'ci_passed'",
        )

    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    if not sync_settings.get("github_repo_url") or not sync_settings.get("github_token"):
        raise HTTPException(status_code=400, detail="GitHub repo URL and token must be configured in sync settings")

    user_email = x_user_email or "unknown"
    try:
        owner, repo = _parse_repo(sync_settings["github_repo_url"])
        token = sync_settings["github_token"]
        files = [SyncFilePayload(path=f["path"], content=f["content"]) for f in (doc.get("files") or [])]

        pr_title = f"[Shielva Sync] {doc['connector_name']} — {x_tenant_id}"
        pr_body = (
            f"## Sync Request\n\n"
            f"- **Connector**: {doc['connector_name']}\n"
            f"- **Tenant**: {x_tenant_id}\n"
            f"- **Raised by**: {user_email}\n"
            f"- **Session**: {doc['session_id']}\n"
            f"- **CI Pipeline**: All gates passed\n\n"
            f"---\n*Created via Shielva Agentic Developer*"
        )
        effective_branch = (body.branch_name if body and body.branch_name else None) or doc["branch_name"]

        # If branch was already pushed during CI (branch_commit_sha is set),
        # skip the branch push step and just open the PR from the existing branch.
        if doc.get("branch_commit_sha"):
            pr_data = await _open_pr(
                owner, repo, token,
                effective_branch, doc["target_branch"],
                pr_title, pr_body,
            )
            pr_data["commit_sha"] = doc["branch_commit_sha"]
        else:
            # Fallback: branch was not pushed during CI (e.g. GitHub not configured then)
            pr_data = await _create_pr(
                owner, repo, token,
                effective_branch, doc["target_branch"],
                pr_title, pr_body,
                files,
            )

        diff = await _get_pr_diff(owner, repo, token, pr_data["pr_number"])

        now = datetime.utcnow()
        await col.update_one(
            {"_id": ObjectId(sync_request_id)},
            {"$set": {
                "status": "ready",
                "pr_number": pr_data["pr_number"],
                "pr_url": pr_data["pr_url"],
                "pr_state": "open",
                "diff": diff,
                "updated_at": now,
            }},
        )
        _broadcast(x_tenant_id, "sync:request_ready", {
            "sync_request_id": sync_request_id,
            "pr_number": pr_data["pr_number"],
            "pr_url": pr_data["pr_url"],
            "diff": diff,
        })
        return {"status": "ready", "pr_number": pr_data["pr_number"], "pr_url": pr_data["pr_url"]}
    except HTTPException:
        raise
    except Exception as e:
        await col.update_one(
            {"_id": ObjectId(sync_request_id)},
            {"$set": {"status": "error", "error": str(e)[:500], "updated_at": datetime.utcnow()}},
        )
        _broadcast(x_tenant_id, "sync:request_error", {"sync_request_id": sync_request_id, "error": str(e)[:200]})
        raise HTTPException(status_code=500, detail=f"PR creation failed: {str(e)[:200]}")


@sync_request_router.post("/{sync_request_id}/approve")
async def approve_sync_request(
    sync_request_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Approve and merge the PR."""
    user_role = (x_user_role or "viewer").lower()
    user_email = x_user_email or "unknown"

    # Permission check
    perms = SYNC_PERMISSIONS.get(user_role, SYNC_PERMISSIONS["viewer"])
    if not perms["can_approve"]:
        raise HTTPException(status_code=403, detail=f"Role '{user_role}' cannot approve sync requests")

    col = _sync_requests_col()
    try:
        doc = await col.find_one({"_id": ObjectId(sync_request_id), "tenant_id": x_tenant_id})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid sync request ID")
    if not doc:
        raise HTTPException(status_code=404, detail="Sync request not found")
    if doc["status"] != "ready":
        raise HTTPException(status_code=409, detail=f"Cannot approve — status is '{doc['status']}', expected 'ready'")

    # Check authorized approvers
    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    approvers = sync_settings.get("authorized_approvers", [])
    if approvers and user_email not in approvers and user_role != "super_admin":
        raise HTTPException(status_code=403, detail="You are not in the authorized approvers list for this tenant")

    # Atomic status transition: ready → approving (prevents double-approve race)
    update_result = await col.update_one(
        {"_id": ObjectId(sync_request_id), "status": "ready"},
        {"$set": {"status": "approving", "approved_by": user_email, "updated_at": datetime.utcnow()}},
    )
    if update_result.modified_count == 0:
        raise HTTPException(status_code=409, detail="Sync request is no longer in 'ready' state — it may have been approved by someone else")

    _broadcast(x_tenant_id, "sync:request_approving", {"sync_request_id": sync_request_id, "approved_by": user_email})

    # Merge PR
    try:
        owner, repo = _parse_repo(sync_settings["github_repo_url"])
        token = sync_settings["github_token"]

        await _merge_pr(owner, repo, token, doc["pr_number"])
        await _delete_branch(owner, repo, token, doc["branch_name"])

        now = datetime.utcnow()
        await col.update_one(
            {"_id": ObjectId(sync_request_id)},
            {"$set": {"status": "merged", "pr_state": "merged", "merged_at": now, "updated_at": now}},
        )

        _broadcast(x_tenant_id, "sync:request_merged", {
            "sync_request_id": sync_request_id,
            "approved_by": user_email,
            "merged_at": now.isoformat(),
        })

        # Check if queue is now empty
        open_count = await col.count_documents({
            "tenant_id": x_tenant_id,
            "status": {"$nin": ["merged", "dismissed", "error"]},
        })
        if open_count == 0:
            _broadcast(x_tenant_id, "sync:queue_empty", {"tenant_id": x_tenant_id})

        return {"status": "merged", "approved_by": user_email, "merged_at": now.isoformat()}

    except HTTPException:
        raise
    except Exception as e:
        await col.update_one(
            {"_id": ObjectId(sync_request_id)},
            {"$set": {"status": "error", "error": str(e)[:500], "updated_at": datetime.utcnow()}},
        )
        _broadcast(x_tenant_id, "sync:request_error", {"sync_request_id": sync_request_id, "error": str(e)[:200]})
        raise HTTPException(status_code=500, detail=f"Merge failed: {str(e)[:200]}")



class BulkMergeRequest(BaseModel):
    """List of sync_request IDs to merge sequentially on the server."""
    sync_request_ids: List[str]


class BulkMergeResult(BaseModel):
    sync_request_id: str
    ok: bool
    status: Optional[str] = None
    error: Optional[str] = None


@sync_request_router.post("/bulk-merge")
async def bulk_merge_sync_requests(
    body: BulkMergeRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Sequentially merge a batch of sync requests.

    Why this exists: the client used to fire ``POST /{id}/approve`` in a loop,
    which on a batch of ~50 PRs saturated github.com's per-IP concurrent-
    connection cap and surfaced as "too many concurrent connections from this
    IP". One HTTP call from the client + server-side serialisation keeps GitHub
    happy and lets us share auth/permission checks across the batch.

    Per-merge behaviour mirrors the single-PR ``/{id}/approve`` handler:
      - Permission check (caller role + tenant approver list)
      - Atomic Mongo transition ``ready → approving``
      - GitHub merge (squash) — ``_github_request`` already retries 429 with
        exponential backoff
      - Branch cleanup + status flip ``approving → merged``
      - SSE broadcast per merge so the UI updates row-by-row, not all at once
        at the end

    Between rows we sleep ``INTER_MERGE_DELAY_S`` to space requests out for
    GitHub's secondary rate limits even when the 429 retry loop hasn't kicked
    in.
    """
    INTER_MERGE_DELAY_S = 0.5

    user_role = (x_user_role or "viewer").lower()
    user_email = x_user_email or "unknown"
    perms = SYNC_PERMISSIONS.get(user_role, SYNC_PERMISSIONS["viewer"])
    if not perms["can_approve"]:
        raise HTTPException(status_code=403, detail=f"Role '{user_role}' cannot approve sync requests")
    if not body.sync_request_ids:
        return {"results": [], "merged": 0, "failed": 0}

    col = _sync_requests_col()
    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    approvers = sync_settings.get("authorized_approvers", [])
    if approvers and user_email not in approvers and user_role != "super_admin":
        raise HTTPException(status_code=403, detail="You are not in the authorized approvers list for this tenant")
    owner, repo = _parse_repo(sync_settings["github_repo_url"])
    token = sync_settings["github_token"]

    results: List[BulkMergeResult] = []
    merged_count = 0
    failed_count = 0

    for i, sid in enumerate(body.sync_request_ids):
        if i > 0:
            await asyncio.sleep(INTER_MERGE_DELAY_S)
        try:
            try:
                oid = ObjectId(sid)
            except Exception:
                results.append(BulkMergeResult(sync_request_id=sid, ok=False, error="Invalid sync request ID"))
                failed_count += 1
                continue

            doc = await col.find_one({"_id": oid, "tenant_id": x_tenant_id})
            if not doc:
                results.append(BulkMergeResult(sync_request_id=sid, ok=False, error="Sync request not found"))
                failed_count += 1
                continue
            if doc["status"] != "ready":
                results.append(BulkMergeResult(sync_request_id=sid, ok=False, status=doc["status"],
                                               error=f"Cannot approve — status is '{doc['status']}', expected 'ready'"))
                failed_count += 1
                continue

            update_result = await col.update_one(
                {"_id": oid, "status": "ready"},
                {"$set": {"status": "approving", "approved_by": user_email, "updated_at": datetime.utcnow()}},
            )
            if update_result.modified_count == 0:
                results.append(BulkMergeResult(sync_request_id=sid, ok=False,
                                               error="Sync request no longer in 'ready' state"))
                failed_count += 1
                continue

            _broadcast(x_tenant_id, "sync:request_approving",
                       {"sync_request_id": sid, "approved_by": user_email})

            try:
                await _merge_pr(owner, repo, token, doc["pr_number"])
                await _delete_branch(owner, repo, token, doc["branch_name"])
            except HTTPException as gh_exc:
                # Roll back the status so retries are possible.
                await col.update_one(
                    {"_id": oid},
                    {"$set": {"status": "error", "error": str(gh_exc.detail)[:500],
                              "updated_at": datetime.utcnow()}},
                )
                _broadcast(x_tenant_id, "sync:request_error",
                           {"sync_request_id": sid, "error": str(gh_exc.detail)[:200]})
                results.append(BulkMergeResult(sync_request_id=sid, ok=False, error=str(gh_exc.detail)[:200]))
                failed_count += 1
                continue

            now = datetime.utcnow()
            await col.update_one(
                {"_id": oid},
                {"$set": {"status": "merged", "pr_state": "merged",
                          "merged_at": now, "updated_at": now}},
            )
            _broadcast(x_tenant_id, "sync:request_merged", {
                "sync_request_id": sid,
                "approved_by": user_email,
                "merged_at": now.isoformat(),
            })
            results.append(BulkMergeResult(sync_request_id=sid, ok=True, status="merged"))
            merged_count += 1

        except Exception as e:
            logger.error("sync.bulk_merge_unexpected", sync_request_id=sid, error=str(e)[:200], exc_info=True)
            results.append(BulkMergeResult(sync_request_id=sid, ok=False, error=str(e)[:200]))
            failed_count += 1

    # One queue-empty broadcast at the very end (single check instead of per merge).
    try:
        open_count = await col.count_documents({
            "tenant_id": x_tenant_id,
            "status": {"$nin": ["merged", "dismissed", "error"]},
        })
        if open_count == 0:
            _broadcast(x_tenant_id, "sync:queue_empty", {"tenant_id": x_tenant_id})
    except Exception:
        pass

    return {
        "results": [r.model_dump() for r in results],
        "merged": merged_count,
        "failed": failed_count,
    }


@sync_request_router.post("/{sync_request_id}/hold")
async def hold_sync_request(
    sync_request_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Hold the sync queue — blocks new sync requests from being raised.

    Uses atomic find_one_and_update to prevent two users from holding simultaneously.
    """
    user_role = (x_user_role or "viewer").lower()
    user_email = x_user_email or "unknown"

    perms = SYNC_PERMISSIONS.get(user_role, SYNC_PERMISSIONS["viewer"])
    if not perms["can_hold"]:
        raise HTTPException(status_code=403, detail=f"Role '{user_role}' cannot hold sync requests")

    col = _sync_requests_col()
    now = datetime.utcnow()

    # Atomic hold: only succeeds if held_by is currently None
    result = await col.find_one_and_update(
        {
            "_id": ObjectId(sync_request_id),
            "tenant_id": x_tenant_id,
            "held_by": None,
        },
        {"$set": {"held_by": user_email, "held_at": now, "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )

    if result is None:
        # Either doesn't exist or already held
        doc = await col.find_one({"_id": ObjectId(sync_request_id), "tenant_id": x_tenant_id})
        if not doc:
            raise HTTPException(status_code=404, detail="Sync request not found")
        raise HTTPException(
            status_code=409,
            detail=f"Queue is already held by {doc.get('held_by', 'unknown')}",
        )

    _broadcast(x_tenant_id, "sync:queue_held", {
        "sync_request_id": sync_request_id,
        "held_by": user_email,
    })

    return {"status": "held", "held_by": user_email}


@sync_request_router.post("/{sync_request_id}/unhold")
async def unhold_sync_request(
    sync_request_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Release the hold on the sync queue."""
    user_role = (x_user_role or "viewer").lower()
    user_email = x_user_email or "unknown"

    col = _sync_requests_col()
    try:
        doc = await col.find_one({"_id": ObjectId(sync_request_id), "tenant_id": x_tenant_id})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid sync request ID")
    if not doc:
        raise HTTPException(status_code=404, detail="Sync request not found")

    # Only the holder or super_admin can unhold
    if doc.get("held_by") != user_email and user_role != "super_admin":
        raise HTTPException(status_code=403, detail="Only the holder or super_admin can unhold")

    await col.update_one(
        {"_id": ObjectId(sync_request_id)},
        {"$set": {"held_by": None, "held_at": None, "updated_at": datetime.utcnow()}},
    )

    _broadcast(x_tenant_id, "sync:queue_unheld", {
        "sync_request_id": sync_request_id,
        "unheld_by": user_email,
    })

    return {"status": "unheld"}


@sync_request_router.post("/{sync_request_id}/dismiss")
async def dismiss_sync_request(
    sync_request_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
):
    """Dismiss a sync request without merging. Closes the GitHub PR if one exists."""
    col = _sync_requests_col()
    try:
        doc = await col.find_one({"_id": ObjectId(sync_request_id), "tenant_id": x_tenant_id})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid sync request ID")
    if not doc:
        raise HTTPException(status_code=404, detail="Sync request not found")

    # Close GitHub PR if exists
    pr_closed = False
    if doc.get("pr_number"):
        try:
            sync_settings = await _get_tenant_sync_settings(x_tenant_id)
            owner, repo = _parse_repo(sync_settings["github_repo_url"])
            await _close_pr(owner, repo, sync_settings["github_token"], doc["pr_number"])
            pr_closed = True
        except Exception:
            pass  # Non-critical

    _dismiss_set: Dict[str, Any] = {"status": "dismissed", "updated_at": datetime.utcnow()}
    if pr_closed:
        _dismiss_set["pr_state"] = "closed"   # so the card shows "PR closed", not "PR open"
    await col.update_one(
        {"_id": ObjectId(sync_request_id)},
        {"$set": _dismiss_set},
    )

    _broadcast(x_tenant_id, "sync:request_dismissed", {"sync_request_id": sync_request_id})
    if pr_closed:
        _broadcast(x_tenant_id, "sync:pr_state", {"sync_request_id": sync_request_id, "pr_state": "closed"})

    # Check if queue is now empty
    open_count = await col.count_documents({
        "tenant_id": x_tenant_id,
        "status": {"$nin": ["merged", "dismissed", "error"]},
    })
    if open_count == 0:
        _broadcast(x_tenant_id, "sync:queue_empty", {"tenant_id": x_tenant_id})

    return {"status": "dismissed"}


# ── Sync Settings endpoints ──────────────────────────────────────────────────

@sync_request_router.get("/settings/config")
async def get_sync_settings(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Get tenant sync configuration."""
    doc = await _get_tenant_sync_settings(x_tenant_id)
    # Never expose the token to the client — just indicate if it's set
    has_token = bool(doc.get("github_token"))
    doc.pop("github_token", None)
    doc["has_github_token"] = has_token
    return doc


@sync_request_router.put("/settings/config")
async def update_sync_settings(
    body: SyncSettingsBody,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Update tenant sync configuration."""
    user_role = (x_user_role or "viewer").lower()
    perms = SYNC_PERMISSIONS.get(user_role, SYNC_PERMISSIONS["viewer"])

    # Only users with can_configure_approvers can update approvers
    if body.authorized_approvers is not None and not perms["can_configure_approvers"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions to configure approvers")

    col = _sync_settings_col()
    update_fields: Dict[str, Any] = {"updated_at": datetime.utcnow()}

    if body.github_repo_url is not None:
        update_fields["github_repo_url"] = body.github_repo_url
    if body.github_token is not None:
        # Encrypt token before storing
        update_fields["github_token"] = _encrypt_token(body.github_token)
    if body.default_target_branch is not None:
        update_fields["default_target_branch"] = body.default_target_branch
    if body.authorized_approvers is not None:
        update_fields["authorized_approvers"] = body.authorized_approvers

    await col.update_one(
        {"tenant_id": x_tenant_id},
        {"$set": update_fields, "$setOnInsert": {"tenant_id": x_tenant_id}},
        upsert=True,
    )

    return {"status": "updated"}


# ── Pull & Reload endpoint (manual trigger from frontend) ───────────────────

@sync_request_router.post("/pull-and-reload")
async def pull_and_reload(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
):
    """Manually trigger a git pull + connector hot-reload via the gateway API.

    Uses the tenant's configured PAT for authenticated HTTPS pull.
    Only super_admin and tenant_admin can trigger this.
    """
    import httpx

    user_role = (x_user_role or "viewer").lower()
    if user_role not in ("super_admin", "tenant_admin", "admin"):
        raise HTTPException(
            status_code=403,
            detail=f"Role '{user_role}' cannot trigger pull-and-reload. Requires super_admin or tenant_admin.",
        )

    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    token = sync_settings.get("github_token", "")
    repo_url = sync_settings.get("github_repo_url", "")
    target_branch = sync_settings.get("default_target_branch", "connector-development")

    if not token or not repo_url:
        raise HTTPException(
            status_code=400,
            detail="GitHub token and repo URL must be configured in sync settings.",
        )

    gateway_url = getattr(settings, "CONNECTOR_GATEWAY_URL", "https://localhost:8003")

    try:
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            resp = await client.post(
                f"{gateway_url}/internal/pull-and-reload",
                json={
                    "target_branch": target_branch,
                    "github_token": token,
                    "github_repo_url": repo_url,
                },
                headers={"X-User-Role": user_role},
            )
            data = resp.json() if resp.status_code == 200 else {"error": resp.text[:200]}
    except Exception as e:
        data = {"error": str(e)[:200]}

    # Broadcast to all tenant clients
    _broadcast(x_tenant_id, "sync:code_pulled", {
        "tenant_id": x_tenant_id,
        "branch": target_branch,
        "triggered_by": x_user_email or "unknown",
        "git_pull": data.get("git_pull"),
        "reload": data.get("reload"),
    })

    return data


# ── Webhook management endpoints ────────────────────────────────────────────

class WebhookConfigBody(BaseModel):
    webhook_url: str  # The public URL where GitHub should send events


_WEBHOOK_PATH_SUFFIX = "/api/v3/sync-webhooks/github"


async def _cleanup_stale_webhooks(owner: str, repo: str, token: str, keep_hook_id) -> int:
    """Delete every OTHER Shielva sync webhook on the repo except ``keep_hook_id``.

    Ephemeral Cloudflare quick-tunnels hand out a new hostname on each restart, so
    each tunnel start used to register a fresh hook and leave the old (now-dead) one
    behind — they pile up and every one fails delivery with 502. We identify ours by
    the fixed path suffix and prune all but the one we just (re)registered.
    """
    try:
        hooks = await _github_request("GET", f"/repos/{owner}/{repo}/hooks", token)
    except HTTPException:
        return 0
    removed = 0
    for h in hooks if isinstance(hooks, list) else []:
        hid = h.get("id")
        url = h.get("config", {}).get("url", "")
        if hid != keep_hook_id and url.endswith(_WEBHOOK_PATH_SUFFIX):
            try:
                await _github_request("DELETE", f"/repos/{owner}/{repo}/hooks/{hid}", token)
                removed += 1
            except HTTPException as exc:
                logger.warning("webhook.stale_delete_failed", hook_id=hid, status=exc.status_code)
    if removed:
        logger.info("webhook.stale_cleaned", owner=owner, repo=repo, removed=removed, kept=keep_hook_id)
    return removed


@sync_request_router.post("/webhook/register")
async def register_webhook(
    body: WebhookConfigBody,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Auto-register a GitHub webhook on the repo using the tenant's PAT.

    - Generates a random webhook secret
    - Creates the webhook on GitHub via ``POST /repos/{owner}/{repo}/hooks``
    - Stores the webhook secret + hook ID in sync settings (encrypted)
    - Stores the webhook URL in sync settings for display

    Requires: PAT with ``admin:repo_hook`` (classic) or ``Webhooks: Read & Write`` (fine-grained).
    Only super_admin / tenant_admin can register.
    """
    import secrets as _secrets

    user_role = (x_user_role or "viewer").lower()
    perms = SYNC_PERMISSIONS.get(user_role, SYNC_PERMISSIONS["viewer"])
    if not perms["can_configure_approvers"]:
        raise HTTPException(status_code=403, detail=f"Role '{user_role}' cannot configure webhooks")

    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    token = sync_settings.get("github_token", "")
    repo_url = sync_settings.get("github_repo_url", "")

    if not token or not repo_url:
        raise HTTPException(status_code=400, detail="GitHub token and repo URL must be configured first.")

    owner, repo = _parse_repo(repo_url)

    # Generate a random webhook secret (32 bytes, hex-encoded)
    webhook_secret = _secrets.token_hex(32)

    # Check if a webhook already exists for this URL — avoid duplicates
    existing_hooks = await _github_request("GET", f"/repos/{owner}/{repo}/hooks", token)
    for hook in existing_hooks if isinstance(existing_hooks, list) else []:
        hook_url = hook.get("config", {}).get("url", "")
        if hook_url == body.webhook_url:
            # Already registered — update the secret instead
            hook_id = hook["id"]
            await _github_request("PATCH", f"/repos/{owner}/{repo}/hooks/{hook_id}", token, {
                "config": {
                    "url": body.webhook_url,
                    "content_type": "json",
                    "secret": webhook_secret,
                    "insecure_ssl": "0",
                },
                "events": ["pull_request", "pull_request_review"],
                "active": True,
            })
            # Store webhook config
            col = _sync_settings_col()
            await col.update_one(
                {"tenant_id": x_tenant_id},
                {"$set": {
                    "webhook_url": body.webhook_url,
                    "webhook_secret": _encrypt_token(webhook_secret),
                    "webhook_hook_id": hook_id,
                    "webhook_active": True,
                    "updated_at": datetime.utcnow(),
                }},
                upsert=True,
            )
            # Also update the server config so HMAC verification uses the new secret
            settings.GITHUB_WEBHOOK_SECRET = webhook_secret
            # Prune any other stale Shielva hooks (old tunnel URLs)
            await _cleanup_stale_webhooks(owner, repo, token, hook_id)
            return {
                "status": "updated",
                "hook_id": hook_id,
                "webhook_url": body.webhook_url,
                "message": "Webhook updated with new secret.",
            }

    # Create new webhook
    hook_data = await _github_request("POST", f"/repos/{owner}/{repo}/hooks", token, {
        "name": "web",
        "config": {
            "url": body.webhook_url,
            "content_type": "json",
            "secret": webhook_secret,
            "insecure_ssl": "0",
        },
        "events": ["pull_request", "pull_request_review"],
        "active": True,
    })

    hook_id = hook_data.get("id")

    # Store webhook config in sync settings
    col = _sync_settings_col()
    await col.update_one(
        {"tenant_id": x_tenant_id},
        {"$set": {
            "webhook_url": body.webhook_url,
            "webhook_secret": _encrypt_token(webhook_secret),
            "webhook_hook_id": hook_id,
            "webhook_active": True,
            "updated_at": datetime.utcnow(),
        }},
        upsert=True,
    )
    # Update server config for HMAC verification
    settings.GITHUB_WEBHOOK_SECRET = webhook_secret

    # Prune any other stale Shielva hooks (old tunnel URLs) so they stop piling up.
    await _cleanup_stale_webhooks(owner, repo, token, hook_id)

    return {
        "status": "created",
        "hook_id": hook_id,
        "webhook_url": body.webhook_url,
        "message": "Webhook registered on GitHub. Events: pull_request, pull_request_review.",
    }


@sync_request_router.get("/webhook/status")
async def webhook_status(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Check whether the GitHub webhook is configured and active.

    Returns:
      - webhook_url: the configured URL (or null)
      - active: whether GitHub reports the hook as active
      - last_delivery: timestamp of last successful delivery (if available)
    """
    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    token = sync_settings.get("github_token", "")
    repo_url = sync_settings.get("github_repo_url", "")
    hook_id = sync_settings.get("webhook_hook_id")
    webhook_url = sync_settings.get("webhook_url", "")

    if not hook_id or not token or not repo_url:
        return {
            "configured": False,
            "webhook_url": webhook_url or None,
            "active": False,
            "last_delivery": None,
            "message": "Webhook not configured. Use 'Configure Webhook' to set it up.",
        }

    # Verify the hook still exists on GitHub
    owner, repo = _parse_repo(repo_url)
    try:
        hook = await _github_request("GET", f"/repos/{owner}/{repo}/hooks/{hook_id}", token)
        active = hook.get("active", False)
        last_response = hook.get("last_response", {})
        last_code = last_response.get("code")
        last_msg = last_response.get("message", "")
        return {
            "configured": True,
            "webhook_url": hook.get("config", {}).get("url", webhook_url),
            "active": active,
            "hook_id": hook_id,
            "events": hook.get("events", []),
            "last_delivery_status": last_code,
            "last_delivery_message": last_msg,
            "message": "Webhook is active." if active else "Webhook exists but is inactive.",
        }
    except HTTPException as e:
        if e.status_code == 404:
            # Hook was deleted from GitHub — clean up local state
            col = _sync_settings_col()
            await col.update_one(
                {"tenant_id": x_tenant_id},
                {"$set": {"webhook_hook_id": None, "webhook_active": False, "updated_at": datetime.utcnow()}},
            )
            return {
                "configured": False,
                "webhook_url": webhook_url,
                "active": False,
                "message": "Webhook was removed from GitHub. Re-configure to restore.",
            }
        raise


@sync_request_router.delete("/webhook/remove")
async def remove_webhook(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Remove the GitHub webhook from the repo and clear local config."""
    user_role = (x_user_role or "viewer").lower()
    perms = SYNC_PERMISSIONS.get(user_role, SYNC_PERMISSIONS["viewer"])
    if not perms["can_configure_approvers"]:
        raise HTTPException(status_code=403, detail=f"Role '{user_role}' cannot remove webhooks")

    sync_settings = await _get_tenant_sync_settings(x_tenant_id)
    token = sync_settings.get("github_token", "")
    repo_url = sync_settings.get("github_repo_url", "")
    hook_id = sync_settings.get("webhook_hook_id")

    if hook_id and token and repo_url:
        try:
            owner, repo = _parse_repo(repo_url)
            await _github_request("DELETE", f"/repos/{owner}/{repo}/hooks/{hook_id}", token)
        except Exception:
            pass  # hook may already be deleted

    # Clear local state
    col = _sync_settings_col()
    await col.update_one(
        {"tenant_id": x_tenant_id},
        {"$set": {
            "webhook_hook_id": None,
            "webhook_secret": None,
            "webhook_active": False,
            "webhook_url": None,
            "updated_at": datetime.utcnow(),
        }},
    )
    settings.GITHUB_WEBHOOK_SECRET = ""

    return {"status": "removed", "message": "Webhook removed from GitHub and local config cleared."}
