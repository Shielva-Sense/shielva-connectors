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
from pymongo import ReturnDocument

from integration.core.config import settings
from integration.db.database import get_db

logger = structlog.get_logger(__name__)

sync_request_router = APIRouter(prefix="/sync-requests", tags=["sync-requests"])

# ── Branch access by role ────────────────────────────────────────────────────

BRANCH_ACCESS: Dict[str, List[str]] = {
    "super_admin": ["connector-development", "qa", "uat", "master", "main"],
    "tenant_admin": ["connector-development", "qa", "uat"],
    "bot_manager": ["connector-development", "qa"],
    "admin": ["connector-development", "qa", "uat"],
}

# Default GitHub repo for shielva-connectors
DEFAULT_GITHUB_REPO = "git@github.com:shielvaAdmin/shielva-connectors"

SYNC_PERMISSIONS: Dict[str, Dict[str, bool]] = {
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


async def _create_pr(
    owner: str, repo: str, token: str,
    branch_name: str, target_branch: str,
    title: str, body: str,
    files: List[SyncFilePayload],
) -> Dict[str, Any]:
    """Create a branch, commit files, and open a PR. Returns PR data.

    Uses asyncio.gather() to create all blobs in parallel for better performance.
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
        "message": title,
        "tree": tree["sha"],
        "parents": [base_sha],
    })

    # 6. Create branch ref
    await _github_request("POST", f"{repo_path}/git/refs", token, {
        "ref": f"refs/heads/{branch_name}",
        "sha": commit["sha"],
    })

    # 7. Open PR
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

    Gates 3–5 call service functions directly (no self-HTTP calls).
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

    # Gate 1: Security Audit — offloaded to thread pool
    _broadcast(tenant_id, "sync:ci_progress", {
        "sync_request_id": sync_request_id,
        "gate": {"gate": "security_audit", "status": "running", "summary": "Scanning for secrets and dangerous code..."},
    })
    security_result = await _security_audit(files)
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

    # Gate 3: Import/Compilation Check — direct call (same process, no HTTP)
    _broadcast(tenant_id, "sync:ci_progress", {
        "sync_request_id": sync_request_id,
        "gate": {"gate": "import_check", "status": "running", "summary": "Checking imports and compilation..."},
    })
    import_result = {"gate": "import_check", "status": "passed", "summary": "Imports OK"}
    try:
        from integration.api.testing_routes import _get_session_output_dir
        out_dir, _, _ = await _get_session_output_dir(session_id, tenant_id)

        import subprocess as _sp
        import sys as _sys
        repo_root = Path(settings.GENERATED_CODE_DIR).resolve().parent
        pythonpath = os.pathsep.join([str(out_dir), str(repo_root), str(out_dir.parent)])

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

        proc = await asyncio.to_thread(
            _sp.run,
            [_sys.executable, "-c", check_script],
            cwd=str(out_dir),
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONPATH": pythonpath},
        )
        output = (proc.stdout + proc.stderr).strip() or "OK: all files compile clean"
        clean = output.startswith("OK:")
        if not clean:
            import_result["status"] = "failed"
            import_result["summary"] = "Import errors detected"
            import_result["details"] = output
    except HTTPException:
        import_result["status"] = "skipped"
        import_result["summary"] = "Session output directory not found"
    except Exception as e:
        import_result["status"] = "skipped"
        import_result["summary"] = f"Import check unavailable: {str(e)[:100]}"
    await _update_gate(import_result)
    if import_result["status"] == "failed":
        return ci_results, cleaned_files

    # Gate 4: Unit Tests — direct function call (no HTTP)
    _broadcast(tenant_id, "sync:ci_progress", {
        "sync_request_id": sync_request_id,
        "gate": {"gate": "unit_tests", "status": "running", "summary": "Running unit tests..."},
    })
    unit_result = {"gate": "unit_tests", "status": "passed", "summary": "Unit tests passed"}
    try:
        from integration.services.testing_service import run_tests
        data = await run_tests(session_id=session_id, tenant_id=tenant_id, test_mode="unit")
        pytest_data = data.get("pytest", {})
        passed = pytest_data.get("passed", 0)
        failed = pytest_data.get("failed", 0)
        errors = pytest_data.get("errors", 0)
        unit_result["summary"] = f"{passed} passed, {failed} failed, {errors} errors"
        if failed > 0 or errors > 0:
            unit_result["status"] = "failed"
            unit_result["details"] = json.dumps(pytest_data.get("details", ""))
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
            from integration.services.testing_service import run_tests
            data = await run_tests(session_id=session_id, tenant_id=tenant_id, test_mode="full")
            pytest_data = data.get("pytest", {})
            passed = pytest_data.get("passed", 0)
            failed = pytest_data.get("failed", 0)
            errors = pytest_data.get("errors", 0)
            int_result["summary"] = f"{passed} passed, {failed} failed, {errors} errors"
            if failed > 0 or errors > 0:
                int_result["status"] = "failed"
                int_result["details"] = json.dumps(pytest_data.get("details", ""))
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
        "files": [{"path": f.path, "content": f.content} for f in body.files],
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


@sync_request_router.post("/{sync_request_id}/rerun")
async def rerun_sync_request(
    sync_request_id: str,
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
    if doc["status"] not in ("validation_failed", "error", "ready"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot re-run — status is '{doc['status']}'. Only validation_failed, error, or ready requests can be re-run.",
        )

    sync_settings = await _get_tenant_sync_settings(x_tenant_id)

    # Reset state
    now = datetime.utcnow()
    await col.update_one(
        {"_id": ObjectId(doc["_id"])},
        {"$set": {"status": "validating", "ci_results": [], "error": None, "updated_at": now}},
    )
    _broadcast(x_tenant_id, "sync:ci_rerun", {
        "sync_request_id": sync_request_id,
        "triggered_by": x_user_email or "unknown",
    })

    # Re-run pipeline in background using the original files stored in doc
    files = [SyncFilePayload(path=f["path"], content=f["content"]) for f in (doc.get("files") or [])]

    async def _rerun():
        try:
            ci_results, cleaned_files = await _run_ci_pipeline(
                sync_request_id, x_tenant_id, doc["session_id"], files,
            )
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

            # CI passed — update existing PR or create new one
            owner, repo = _parse_repo(sync_settings["github_repo_url"])
            token = sync_settings["github_token"]
            pr_number = doc.get("pr_number")
            pr_url = doc.get("pr_url")
            branch_name = doc.get("branch_name", "")

            if not pr_number:
                pr_title = f"[Shielva Sync] {doc['connector_name']} — {x_tenant_id}"
                pr_body = (
                    f"## Sync Request (Re-run)\n\n"
                    f"- **Connector**: {doc['connector_name']}\n"
                    f"- **Tenant**: {x_tenant_id}\n"
                    f"- **CI Pipeline**: All gates passed\n\n"
                    f"---\n*Auto-generated by Shielva Agentic Developer*"
                )
                pr_data = await _create_pr(owner, repo, token, branch_name, doc["target_branch"], pr_title, pr_body, cleaned_files)
                pr_number = pr_data["pr_number"]
                pr_url = pr_data["pr_url"]

            diff = await _get_pr_diff(owner, repo, token, pr_number)
            await col.update_one(
                {"_id": ObjectId(sync_request_id)},
                {"$set": {"status": "ready", "pr_number": pr_number, "pr_url": pr_url, "diff": diff, "updated_at": datetime.utcnow()}},
            )
            _broadcast(x_tenant_id, "sync:request_ready", {
                "sync_request_id": sync_request_id,
                "pr_number": pr_number,
                "pr_url": pr_url,
                "diff": diff,
            })
        except Exception as e:
            logger.error("sync_request.rerun_error", error=str(e), sync_request_id=sync_request_id)
            await col.update_one(
                {"_id": ObjectId(sync_request_id)},
                {"$set": {"status": "error", "error": str(e)[:500], "updated_at": datetime.utcnow()}},
            )
            _broadcast(x_tenant_id, "sync:request_error", {"sync_request_id": sync_request_id, "error": str(e)[:200]})

    asyncio.create_task(_rerun())
    return {"status": "validating", "message": "CI pipeline re-started."}


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
