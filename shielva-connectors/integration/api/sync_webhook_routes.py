"""GitHub webhook receiver for sync request PR events.

Handles real-time status updates from GitHub when PRs are merged, closed,
reviewed, or updated externally. Broadcasts SSE events to all connected
Electron clients for the affected tenant.

Registered at: POST /api/v3/sync-webhooks/github
"""

import hashlib
import hmac
import json
from datetime import datetime
from typing import Optional

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


def _verify_github_signature(payload: bytes, signature: Optional[str]) -> bool:
    """Verify HMAC-SHA256 signature from GitHub webhook.

    Fail-closed: if GITHUB_WEBHOOK_SECRET is not set, reject ALL webhooks.
    """
    secret = getattr(settings, "GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        logger.warning(
            "sync_webhook_rejected_no_secret",
            reason="GITHUB_WEBHOOK_SECRET not configured — refusing webhook",
        )
        return False
    if not signature:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@sync_webhook_router.post("/github")
async def github_sync_webhook(
    request: Request,
    x_github_event: Optional[str] = Header(None, alias="X-GitHub-Event"),
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
):
    """Handle GitHub webhook events for sync request PRs."""
    body = await request.body()

    if not _verify_github_signature(body, x_hub_signature_256):
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
            else:
                # PR was closed without merging (externally)
                if doc["status"] not in ("merged", "dismissed"):
                    await col.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"status": "dismissed", "updated_at": datetime.utcnow()}},
                    )
                    _broadcast(tenant_id, "sync:request_dismissed", {
                        "sync_request_id": sync_request_id,
                        "reason": "closed_externally",
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
