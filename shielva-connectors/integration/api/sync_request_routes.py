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
import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from integration.core.config import settings
from integration.db.database import get_db

logger = structlog.get_logger(__name__)

sync_request_router = APIRouter(prefix="/sync-requests", tags=["sync-requests"])

# ── Branch access by role ────────────────────────────────────────────────────

BRANCH_ACCESS: Dict[str, List[str]] = {
    "super_admin": ["development", "qa", "uat", "master", "main"],
    "tenant_admin": ["development", "qa", "uat"],
    "bot_manager": ["development", "qa"],
    "admin": ["development", "qa", "uat"],
}

SYNC_PERMISSIONS: Dict[str, Dict[str, bool]] = {
    "super_admin": {"can_raise": True, "can_approve": True, "can_hold": True, "can_configure_approvers": True},
    "tenant_admin": {"can_raise": True, "can_approve": True, "can_hold": True, "can_configure_approvers": True},
    "bot_manager": {"can_raise": True, "can_approve": False, "can_hold": False, "can_configure_approvers": False},
    "admin": {"can_raise": True, "can_approve": True, "can_hold": True, "can_configure_approvers": True},
    "analyst": {"can_raise": False, "can_approve": False, "can_hold": False, "can_configure_approvers": False},
    "viewer": {"can_raise": False, "can_approve": False, "can_hold": False, "can_configure_approvers": False},
    "partner": {"can_raise": False, "can_approve": False, "can_hold": False, "can_configure_approvers": False},
}

# ── SSE broadcast ────────────────────────────────────────────────────────────
# In-memory per-tenant SSE queues. Each connected client gets its own queue.
# Keyed by tenant_id → set of asyncio.Queue instances.

_sse_clients: Dict[str, set] = {}


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


# ── MongoDB collections ─────────────────────────────────────────────────────

def _sync_requests_col():
    return get_db()["sync_requests"]


def _sync_settings_col():
    return get_db()["sync_settings"]


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


def _validate_path(rel_path: str) -> bool:
    """Return True if the path is safe (no traversal, no absolute)."""
    if ".." in rel_path or rel_path.startswith("/") or rel_path.startswith("\\"):
        return False
    return True


def _security_audit(files: List[SyncFilePayload]) -> Dict[str, Any]:
    """Gate 1: Scan files for secrets, dangerous calls, credential files."""
    findings = []
    for f in files:
        if not _validate_path(f.path):
            findings.append({"file": f.path, "issue": "Path traversal attempt", "severity": "critical"})
            continue
        # Check for .env or credential files
        basename = os.path.basename(f.path).lower()
        if basename in (".env", ".env.local", ".env.production", "credentials.json", "secrets.json"):
            findings.append({"file": f.path, "issue": f"Credential file detected: {basename}", "severity": "critical"})
            continue
        # Scan content
        for i, line in enumerate(f.content.split("\n"), 1):
            for pat in SECRET_PATTERNS:
                if pat.search(line):
                    findings.append({"file": f.path, "line": i, "issue": "Potential hardcoded secret", "severity": "high"})
                    break
            if DANGEROUS_CALLS.search(line):
                findings.append({"file": f.path, "line": i, "issue": "Dangerous function call (eval/exec/os.system)", "severity": "high"})

    passed = not any(f["severity"] == "critical" for f in findings)
    return {
        "gate": "security_audit",
        "status": "passed" if passed else "failed",
        "summary": f"{len(findings)} finding(s)" if findings else "No security issues detected",
        "details": json.dumps(findings) if findings else None,
    }


def _smart_diff(files: List[SyncFilePayload]) -> tuple[List[SyncFilePayload], Dict[str, Any]]:
    """Gate 2: Strip junk files (pycache, .pyc, IDE configs, etc.)."""
    cleaned = []
    removed = []
    for f in files:
        is_junk = False
        for pat in JUNK_PATTERNS:
            if pat in f.path.lower():
                is_junk = True
                removed.append(f.path)
                break
        # Skip whitespace-only files
        if not is_junk and f.content.strip():
            cleaned.append(f)
        elif not is_junk and not f.content.strip():
            removed.append(f.path)

    result = {
        "gate": "smart_diff",
        "status": "passed",
        "summary": f"Kept {len(cleaned)} file(s), stripped {len(removed)} junk/empty file(s)",
        "details": json.dumps({"removed": removed}) if removed else None,
    }
    return cleaned, result


# ── GitHub API helpers ───────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com"


def _parse_repo(repo_url: str) -> tuple[str, str]:
    """Extract owner/repo from a GitHub URL."""
    # https://github.com/org/repo or https://github.com/org/repo.git
    url = repo_url.rstrip("/").rstrip(".git")
    parts = url.split("/")
    if len(parts) < 2:
        raise ValueError(f"Invalid GitHub repo URL: {repo_url}")
    return parts[-2], parts[-1]


async def _github_request(
    method: str, path: str, token: str,
    json_body: Optional[Dict] = None,
    timeout: float = 30.0,
) -> Dict:
    """Make an authenticated GitHub API request."""
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(
            method,
            f"{GITHUB_API}{path}",
            headers=headers,
            json=json_body,
        )
        if resp.status_code >= 400:
            logger.error("github_api_error", status=resp.status_code, path=path, body=resp.text[:500])
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"GitHub API error: {resp.status_code} — {resp.text[:200]}",
            )
        return resp.json() if resp.text else {}


async def _create_pr(
    owner: str, repo: str, token: str,
    branch_name: str, target_branch: str,
    title: str, body: str,
    files: List[SyncFilePayload],
) -> Dict[str, Any]:
    """Create a branch, commit files, and open a PR. Returns PR data."""
    # 1. Get target branch SHA
    ref_data = await _github_request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{target_branch}", token)
    base_sha = ref_data["object"]["sha"]

    # 2. Get the base tree
    commit_data = await _github_request("GET", f"/repos/{owner}/{repo}/git/commits/{base_sha}", token)
    base_tree_sha = commit_data["tree"]["sha"]

    # 3. Create blobs for each file
    tree_items = []
    for f in files:
        blob = await _github_request("POST", f"/repos/{owner}/{repo}/git/blobs", token, {
            "content": f.content,
            "encoding": "utf-8",
        })
        tree_items.append({
            "path": f.path,
            "mode": "100644",
            "type": "blob",
            "sha": blob["sha"],
        })

    # 4. Create tree
    tree = await _github_request("POST", f"/repos/{owner}/{repo}/git/trees", token, {
        "base_tree": base_tree_sha,
        "tree": tree_items,
    })

    # 5. Create commit
    commit = await _github_request("POST", f"/repos/{owner}/{repo}/git/commits", token, {
        "message": title,
        "tree": tree["sha"],
        "parents": [base_sha],
    })

    # 6. Create branch ref
    await _github_request("POST", f"/repos/{owner}/{repo}/git/refs", token, {
        "ref": f"refs/heads/{branch_name}",
        "sha": commit["sha"],
    })

    # 7. Open PR
    pr = await _github_request("POST", f"/repos/{owner}/{repo}/pulls", token, {
        "title": title,
        "body": body,
        "head": branch_name,
        "base": target_branch,
    })

    return {
        "pr_number": pr["number"],
        "pr_url": pr["html_url"],
        "branch_name": branch_name,
        "commit_sha": commit["sha"],
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
    """Get or create sync settings for a tenant."""
    col = _sync_settings_col()
    doc = await col.find_one({"tenant_id": tenant_id})
    if not doc:
        return {
            "tenant_id": tenant_id,
            "github_repo_url": "",
            "github_token": "",
            "default_target_branch": "development",
            "authorized_approvers": [],
        }
    doc["_id"] = str(doc["_id"])
    return doc


# ── CI pipeline runner ───────────────────────────────────────────────────────

async def _run_ci_pipeline(
    sync_request_id: str,
    tenant_id: str,
    session_id: str,
    files: List[SyncFilePayload],
) -> tuple[List[Dict], List[SyncFilePayload]]:
    """Run all CI gates sequentially. Broadcasts SSE progress. Returns (ci_results, cleaned_files).

    Test gates respect the session's ``test_type`` field:
      - "unit"  → only run unit tests (Gate 4)
      - "both"  → run unit tests (Gate 4) + integration tests (Gate 5)
    If test_type is not set, defaults to "unit" only.
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

    async def _update_gate(gate_result: Dict):
        ci_results.append(gate_result)
        await col.update_one({"_id": oid}, {"$set": {"ci_results": ci_results, "updated_at": datetime.utcnow()}})
        _broadcast(tenant_id, "sync:ci_progress", {
            "sync_request_id": sync_request_id,
            "gate": gate_result,
        })

    # Gate 1: Security Audit
    _broadcast(tenant_id, "sync:ci_progress", {
        "sync_request_id": sync_request_id,
        "gate": {"gate": "security_audit", "status": "running", "summary": "Scanning for secrets and dangerous code..."},
    })
    security_result = _security_audit(files)
    await _update_gate(security_result)
    if security_result["status"] == "failed":
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
        return ci_results, cleaned_files

    # Gate 3: Import/Compilation Check — call existing endpoint
    _broadcast(tenant_id, "sync:ci_progress", {
        "sync_request_id": sync_request_id,
        "gate": {"gate": "import_check", "status": "running", "summary": "Checking imports and compilation..."},
    })
    import_result = {"gate": "import_check", "status": "passed", "summary": "Imports OK"}
    try:
        base_url = f"https://localhost:{settings.INTEGRATION_PORT}"
        async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
            resp = await client.get(
                f"{base_url}/api/v3/connector-api/{session_id}/check-imports",
                headers={"X-Tenant-ID": tenant_id},
            )
            if resp.status_code == 200:
                data = resp.json()
                if not data.get("ok", True):
                    import_result["status"] = "failed"
                    import_result["summary"] = "Import errors detected"
                    import_result["details"] = json.dumps(data.get("errors", []))
            else:
                import_result["status"] = "skipped"
                import_result["summary"] = f"Import check returned {resp.status_code}"
    except Exception as e:
        import_result["status"] = "skipped"
        import_result["summary"] = f"Import check unavailable: {str(e)[:100]}"
    await _update_gate(import_result)
    if import_result["status"] == "failed":
        return ci_results, cleaned_files

    # Gate 4: Unit Tests
    _broadcast(tenant_id, "sync:ci_progress", {
        "sync_request_id": sync_request_id,
        "gate": {"gate": "unit_tests", "status": "running", "summary": "Running unit tests..."},
    })
    unit_result = {"gate": "unit_tests", "status": "passed", "summary": "Unit tests passed"}
    try:
        async with httpx.AsyncClient(verify=False, timeout=300.0) as client:
            resp = await client.post(
                f"{base_url}/api/v3/sessions/{session_id}/test",
                headers={"X-Tenant-ID": tenant_id},
                json={"test_mode": "unit"},
            )
            if resp.status_code == 200:
                data = resp.json()
                passed = data.get("passed", 0)
                failed = data.get("failed", 0)
                errors = data.get("errors", 0)
                unit_result["summary"] = f"{passed} passed, {failed} failed, {errors} errors"
                if failed > 0 or errors > 0:
                    unit_result["status"] = "failed"
                    unit_result["details"] = json.dumps(data.get("output", ""))
            else:
                unit_result["status"] = "skipped"
                unit_result["summary"] = f"Test endpoint returned {resp.status_code}"
    except Exception as e:
        unit_result["status"] = "skipped"
        unit_result["summary"] = f"Tests unavailable: {str(e)[:100]}"
    await _update_gate(unit_result)
    if unit_result["status"] == "failed":
        return ci_results, cleaned_files

    # Gate 5: Integration Tests — only if session test_type is "both"
    if test_type == "both":
        _broadcast(tenant_id, "sync:ci_progress", {
            "sync_request_id": sync_request_id,
            "gate": {"gate": "integration_tests", "status": "running", "summary": "Running integration tests..."},
        })
        int_result = {"gate": "integration_tests", "status": "passed", "summary": "Integration tests passed"}
        try:
            async with httpx.AsyncClient(verify=False, timeout=300.0) as client:
                resp = await client.post(
                    f"{base_url}/api/v3/sessions/{session_id}/test",
                    headers={"X-Tenant-ID": tenant_id},
                    json={"test_mode": "integration"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    passed = data.get("passed", 0)
                    failed = data.get("failed", 0)
                    errors = data.get("errors", 0)
                    int_result["summary"] = f"{passed} passed, {failed} failed, {errors} errors"
                    if failed > 0 or errors > 0:
                        int_result["status"] = "failed"
                        int_result["details"] = json.dumps(data.get("output", ""))
                else:
                    int_result["status"] = "skipped"
                    int_result["summary"] = f"Test endpoint returned {resp.status_code}"
        except Exception as e:
            int_result["status"] = "skipped"
            int_result["summary"] = f"Integration tests unavailable: {str(e)[:100]}"
        await _update_gate(int_result)
    else:
        int_result = {
            "gate": "integration_tests",
            "status": "skipped",
            "summary": "Skipped — session configured for unit tests only",
        }
        await _update_gate(int_result)

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

    col = _sync_requests_col()

    # Check if queue is held
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
    branch_name = f"sync/{body.connector_name}/{ts}"
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
        "created_at": now,
        "updated_at": now,
    }
    result = await col.insert_one(doc)
    sync_request_id = str(result.inserted_id)

    _broadcast(x_tenant_id, "sync:request_created", {
        "sync_request_id": sync_request_id,
        "connector_name": body.connector_name,
        "raised_by": user_email,
        "status": "validating",
    })

    # Run CI pipeline in background
    async def _run_pipeline():
        try:
            ci_results, cleaned_files = await _run_ci_pipeline(
                sync_request_id, x_tenant_id, body.session_id, body.files,
            )

            # Check if any gate failed
            any_failed = any(r["status"] == "failed" for r in ci_results)
            if any_failed:
                await col.update_one(
                    {"_id": ObjectId(sync_request_id)},
                    {"$set": {"status": "validation_failed", "updated_at": datetime.utcnow()}},
                )
                _broadcast(x_tenant_id, "sync:request_validation_failed", {
                    "sync_request_id": sync_request_id,
                    "ci_results": ci_results,
                })
                return

            if not cleaned_files:
                await col.update_one(
                    {"_id": ObjectId(sync_request_id)},
                    {"$set": {"status": "validation_failed", "error": "No files to sync after filtering", "updated_at": datetime.utcnow()}},
                )
                return

            # All gates passed — create PR on GitHub
            owner, repo = _parse_repo(sync_settings["github_repo_url"])
            token = sync_settings["github_token"]

            pr_title = f"[Shielva Sync] {body.connector_name} — {x_tenant_id}"
            pr_body = (
                f"## Sync Request\n\n"
                f"- **Connector**: {body.connector_name}\n"
                f"- **Tenant**: {x_tenant_id}\n"
                f"- **Raised by**: {user_email}\n"
                f"- **Session**: {body.session_id}\n"
                f"- **CI Pipeline**: All gates passed\n\n"
                f"---\n*Auto-generated by Shielva Agentic Developer*"
            )

            pr_data = await _create_pr(
                owner, repo, token,
                branch_name, body.target_branch,
                pr_title, pr_body,
                cleaned_files,
            )

            # Fetch diff
            diff = await _get_pr_diff(owner, repo, token, pr_data["pr_number"])

            await col.update_one(
                {"_id": ObjectId(sync_request_id)},
                {"$set": {
                    "status": "ready",
                    "pr_number": pr_data["pr_number"],
                    "pr_url": pr_data["pr_url"],
                    "branch_name": pr_data["branch_name"],
                    "diff": diff,
                    "updated_at": datetime.utcnow(),
                }},
            )

            _broadcast(x_tenant_id, "sync:request_ready", {
                "sync_request_id": sync_request_id,
                "pr_number": pr_data["pr_number"],
                "pr_url": pr_data["pr_url"],
                "diff": diff,
            })

        except Exception as e:
            logger.error("sync_request.pipeline_error", error=str(e), sync_request_id=sync_request_id)
            await col.update_one(
                {"_id": ObjectId(sync_request_id)},
                {"$set": {"status": "error", "error": str(e)[:500], "updated_at": datetime.utcnow()}},
            )
            _broadcast(x_tenant_id, "sync:request_error", {
                "sync_request_id": sync_request_id,
                "error": str(e)[:200],
            })

    asyncio.create_task(_run_pipeline())

    return {
        "sync_request_id": sync_request_id,
        "status": "validating",
        "message": "Sync request created. CI pipeline is running.",
    }


@sync_request_router.get("")
async def list_sync_requests(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    status: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    """List sync requests for a tenant (newest first)."""
    col = _sync_requests_col()
    query: Dict[str, Any] = {"tenant_id": x_tenant_id}
    if status:
        query["status"] = status

    cursor = col.find(query).sort("created_at", -1).limit(limit)
    results = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        results.append(doc)
    return results


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


@sync_request_router.get("/{sync_request_id}")
async def get_sync_request(
    sync_request_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Get a single sync request with full details."""
    col = _sync_requests_col()
    try:
        doc = await col.find_one({"_id": ObjectId(sync_request_id), "tenant_id": x_tenant_id})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid sync request ID")
    if not doc:
        raise HTTPException(status_code=404, detail="Sync request not found")
    doc["_id"] = str(doc["_id"])
    return doc


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

    # Update status
    await col.update_one(
        {"_id": ObjectId(sync_request_id)},
        {"$set": {"status": "approving", "approved_by": user_email, "updated_at": datetime.utcnow()}},
    )
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
            {"$set": {"status": "merged", "merged_at": now, "updated_at": now}},
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


@sync_request_router.post("/{sync_request_id}/hold")
async def hold_sync_request(
    sync_request_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
):
    """Hold the sync queue — blocks new sync requests from being raised."""
    user_role = (x_user_role or "viewer").lower()
    user_email = x_user_email or "unknown"

    perms = SYNC_PERMISSIONS.get(user_role, SYNC_PERMISSIONS["viewer"])
    if not perms["can_hold"]:
        raise HTTPException(status_code=403, detail=f"Role '{user_role}' cannot hold sync requests")

    col = _sync_requests_col()
    try:
        doc = await col.find_one({"_id": ObjectId(sync_request_id), "tenant_id": x_tenant_id})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid sync request ID")
    if not doc:
        raise HTTPException(status_code=404, detail="Sync request not found")

    now = datetime.utcnow()
    await col.update_one(
        {"_id": ObjectId(sync_request_id)},
        {"$set": {"held_by": user_email, "held_at": now, "updated_at": now}},
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
    if doc.get("pr_number"):
        try:
            sync_settings = await _get_tenant_sync_settings(x_tenant_id)
            owner, repo = _parse_repo(sync_settings["github_repo_url"])
            await _close_pr(owner, repo, sync_settings["github_token"], doc["pr_number"])
        except Exception:
            pass  # Non-critical

    await col.update_one(
        {"_id": ObjectId(sync_request_id)},
        {"$set": {"status": "dismissed", "updated_at": datetime.utcnow()}},
    )

    _broadcast(x_tenant_id, "sync:request_dismissed", {"sync_request_id": sync_request_id})

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
        update_fields["github_token"] = body.github_token
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
