"""GitHub webhook receiver for sync request PR events.

Handles real-time status updates from GitHub when PRs are merged, closed,
reviewed, or updated externally. Broadcasts SSE events to all connected
Electron clients for the affected tenant.

On merge to the target branch (e.g. connector-development):
  1. Calls the connector gateway's ``POST /internal/pull-and-reload`` API
     which authenticates via PAT, runs ``git pull --ff-only``, and hot-reloads
     generated connectors — all without a server restart.
  2. Broadcasts ``sync:code_pulled`` SSE event to all tenant clients.

Registered at: POST /api/v3/sync-webhooks/github
"""

import hashlib
import hmac
import json
from datetime import datetime
from typing import Optional

import httpx
import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException, Request

from integration.core.config import settings
from integration.db.database import get_db

# Import the broadcast function from sync_request_routes
from integration.api.sync_request_routes import _broadcast

logger = structlog.get_logger(__name__)

sync_webhook_router = APIRouter(prefix="/sync-webhooks", tags=["sync-webhooks"])


def _sync_requests_col():
    return get_db()["sync_requests"]


async def _verify_github_signature(payload: bytes, signature: Optional[str]) -> bool:
    """Verify HMAC-SHA256 signature from GitHub webhook.

    Checks for the secret in order:
      1. In-memory ``settings.GITHUB_WEBHOOK_SECRET`` (set by register endpoint or env var)
      2. Any tenant's ``webhook_secret`` in MongoDB (for post-restart recovery)

    Fail-closed: if no secret found anywhere, reject ALL webhooks.
    """
    if not signature:
        return False

    # Try in-memory secret first (fastest path)
    secret = getattr(settings, "GITHUB_WEBHOOK_SECRET", "")
    if secret:
        expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature):
            return True

    # Fallback: check all tenants' webhook secrets in MongoDB
    # (handles server restart where in-memory secret is lost)
    from integration.api.sync_request_routes import _sync_settings_col, _decrypt_token
    try:
        col = _sync_settings_col()
        async for doc in col.find({"webhook_secret": {"$ne": None}}, {"webhook_secret": 1}):
            stored_secret = _decrypt_token(doc["webhook_secret"])
            if stored_secret:
                expected = "sha256=" + hmac.new(stored_secret.encode(), payload, hashlib.sha256).hexdigest()
                if hmac.compare_digest(expected, signature):
                    # Cache it in-memory for subsequent requests
                    settings.GITHUB_WEBHOOK_SECRET = stored_secret
                    return True
    except Exception as e:
        logger.warning("sync_webhook.db_secret_lookup_failed", error=str(e)[:100])

    logger.warning(
        "sync_webhook_rejected_no_secret",
        reason="No matching webhook secret found — refusing webhook",
    )
    return False


async def _trigger_pull_and_reload(
    target_branch: str,
    tenant_id: str,
    github_token: str,
    repo_url: str,
    user_role: str = "super_admin",
) -> dict:
    """Call the connector gateway's pull-and-reload API.

    The gateway owns the git repo and connector loading, so it handles:
      1. Authenticated ``git pull --ff-only`` using the PAT
      2. Hot-reload of generated connectors from disk

    This function just makes the API call and broadcasts the result via SSE.
    """
    gateway_url = getattr(settings, "CONNECTOR_GATEWAY_URL", "https://localhost:8003")
    result = {"git_pull": None, "reload": None}

    try:
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            resp = await client.post(
                f"{gateway_url}/internal/pull-and-reload",
                json={
                    "target_branch": target_branch,
                    "github_token": github_token,
                    "github_repo_url": repo_url,
                },
                headers={"X-User-Role": user_role},
            )
            if resp.status_code == 200:
                data = resp.json()
                result = {
                    "git_pull": data.get("git_pull"),
                    "reload": data.get("reload"),
                }
                logger.info(
                    "sync_webhook.pull_and_reload_success",
                    branch=target_branch,
                    git_pull=data.get("git_pull"),
                    reload_loaded=data.get("reload", {}).get("loaded") if isinstance(data.get("reload"), dict) else None,
                )
            elif resp.status_code == 403:
                result["git_pull"] = f"forbidden: {resp.text[:100]}"
                logger.warning("sync_webhook.pull_and_reload_forbidden", status=403, body=resp.text[:200])
            else:
                result["git_pull"] = f"failed: HTTP {resp.status_code}"
                logger.warning("sync_webhook.pull_and_reload_failed", status=resp.status_code, body=resp.text[:200])
    except Exception as e:
        result["git_pull"] = f"error: {str(e)[:100]}"
        logger.error("sync_webhook.pull_and_reload_error", error=str(e)[:200])

    # Broadcast to all tenant clients
    _broadcast(tenant_id, "sync:code_pulled", {
        "tenant_id": tenant_id,
        "branch": target_branch,
        "git_pull": result["git_pull"],
        "reload": result["reload"],
    })

    return result


@sync_webhook_router.post("/github")
async def github_sync_webhook(
    request: Request,
    x_github_event: Optional[str] = Header(None, alias="X-GitHub-Event"),
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    """Handle GitHub webhook events for sync request PRs."""
    body = await request.body()

    if not await _verify_github_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = x_github_event or ""
    action = payload.get("action", "")
    col = _sync_requests_col()

    if event == "pull_request":
        pr_number = payload.get("pull_request", {}).get("number")
        if not pr_number:
            return {"status": "ignored", "reason": "no PR number"}

        # Find the sync request by PR number
        doc = await col.find_one({"pr_number": pr_number})
        if not doc:
            return {"status": "ignored", "reason": "PR not tracked by sync system"}

        tenant_id = doc["tenant_id"]
        sync_request_id = str(doc["_id"])

        if action == "closed":
            merged = payload.get("pull_request", {}).get("merged", False)
            if merged:
                # PR was merged (possibly externally)
                if doc["status"] != "merged":
                    await col.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {
                            "status": "merged",
                            "pr_state": "merged",
                            "merged_at": datetime.utcnow(),
                            "updated_at": datetime.utcnow(),
                        }},
                    )
                    _broadcast(tenant_id, "sync:request_merged", {
                        "sync_request_id": sync_request_id,
                        "approved_by": "github",
                        "merged_at": datetime.utcnow().isoformat(),
                    })

                    # Check if queue is empty
                    open_count = await col.count_documents({
                        "tenant_id": tenant_id,
                        "status": {"$nin": ["merged", "dismissed", "error"]},
                    })
                    if open_count == 0:
                        _broadcast(tenant_id, "sync:queue_empty", {"tenant_id": tenant_id})

                # ── Auto-pull + hot-reload via gateway API ──────────────────
                # Uses the tenant's PAT from sync settings for authenticated pull.
                # The gateway handles git pull + connector reload in one call.
                from integration.api.sync_request_routes import _get_tenant_sync_settings
                sync_settings = await _get_tenant_sync_settings(tenant_id)
                target_branch = doc.get("target_branch", "connector-development")
                # Webhook-triggered pulls run as super_admin (system action)
                pull_result = await _trigger_pull_and_reload(
                    target_branch=target_branch,
                    tenant_id=tenant_id,
                    github_token=sync_settings.get("github_token", ""),
                    repo_url=sync_settings.get("github_repo_url", ""),
                    user_role="super_admin",
                )
                logger.info(
                    "sync_webhook.auto_pull_complete",
                    sync_request_id=sync_request_id,
                    result=pull_result,
                )
            else:
                # PR was closed without merging (externally). Always reflect pr_state=closed
                # (even if already locally dismissed) so the card never shows a stale "PR open".
                if doc["status"] not in ("merged", "dismissed"):
                    await col.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"status": "dismissed", "pr_state": "closed", "updated_at": datetime.utcnow()}},
                    )
                    _broadcast(tenant_id, "sync:request_dismissed", {
                        "sync_request_id": sync_request_id,
                        "reason": "closed_externally",
                    })
                elif doc.get("pr_state") != "closed":
                    await col.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"pr_state": "closed", "updated_at": datetime.utcnow()}},
                    )
                    _broadcast(tenant_id, "sync:pr_state", {
                        "sync_request_id": sync_request_id, "pr_state": "closed",
                    })

        elif action == "synchronize":
            # PR was updated (new commits pushed)
            _broadcast(tenant_id, "sync:request_updated", {
                "sync_request_id": sync_request_id,
                "action": "synchronize",
            })

        elif action == "reopened":
            if doc["status"] == "dismissed":
                await col.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"status": "ready", "updated_at": datetime.utcnow()}},
                )
                _broadcast(tenant_id, "sync:request_ready", {
                    "sync_request_id": sync_request_id,
                    "action": "reopened",
                })

    elif event == "pull_request_review":
        pr_number = payload.get("pull_request", {}).get("number")
        if not pr_number:
            return {"status": "ignored"}

        doc = await col.find_one({"pr_number": pr_number})
        if not doc:
            return {"status": "ignored"}

        review_state = payload.get("review", {}).get("state", "")
        reviewer = payload.get("review", {}).get("user", {}).get("login", "")

        _broadcast(doc["tenant_id"], "sync:review_submitted", {
            "sync_request_id": str(doc["_id"]),
            "reviewer": reviewer,
            "state": review_state,
        })

    return {"status": "processed", "event": event, "action": action}
