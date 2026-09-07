"""Background jobs that survive being on the wrong pod.

🚨 The job store was a dict in one process. At one replica that is correct and
invisible; at two it is a lie with three faces:

  * a status poll load-balanced to the other pod answers "job not found",
  * a cancel reaches a pod that is not running the job and does nothing,
  * dedupe stops working, so the same connector syncs twice concurrently — the
    exact thing dedupe exists to prevent.

None of that shows up until the replica count changes, which is the worst
possible time to discover it. So the store is Redis, with the in-memory dict
kept only as a fallback for a dev machine that has no Redis: there the process
IS the cluster, so the fallback is correct rather than a degraded mode.

Cancellation crosses pods as a FLAG, not as a call. The pod running the job
polls it and cancels its own task — a job can only be interrupted by the process
holding it, and pretending otherwise is how you get a job marked cancelled that
is still running.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

JOB_TTL_S = 1800  # 30 min — long enough for any caller to come back and poll

_KEY = "connectors:job:"
_CANCEL = "connectors:job:cancel:"
_INFLIGHT = "connectors:sync:inflight:"

#: Fallback for a process with no Redis. NOT a cache — the two are alternatives,
#: because a cache would go stale against the other pods and be worse than either.
_LOCAL: dict[str, dict[str, Any]] = {}

TERMINAL = frozenset({"completed", "failed", "cancelled"})


async def _redis():
    from .redis_service import redis_service

    if not redis_service.client:
        await redis_service.connect()
    return redis_service.client


async def put(job_id: str, job: dict[str, Any]) -> None:
    """Write a job record. Fields that start with `_` never leave the process."""
    public = {k: v for k, v in job.items() if not k.startswith("_")}
    client = await _redis()
    if client is None:
        _LOCAL[job_id] = public
        return
    await client.set(_KEY + job_id, json.dumps(public, default=str), ex=JOB_TTL_S)


async def get(job_id: str) -> dict[str, Any] | None:
    client = await _redis()
    if client is None:
        return _LOCAL.get(job_id)
    raw = await client.get(_KEY + job_id)
    return json.loads(raw) if raw else None


async def update(job_id: str, **fields: Any) -> dict[str, Any]:
    """Merge fields into a job. Read-modify-write, which is safe here because a
    job is only ever written by the worker holding it plus a cancel flag that
    lives under its own key precisely so the two never race."""
    job = (await get(job_id)) or {}
    job.update(fields)
    if fields.get("status") in TERMINAL:
        job.setdefault("finished_at", time.time())
    await put(job_id, job)
    return job


async def request_cancel(job_id: str) -> None:
    """Ask whichever pod is running this job to stop."""
    client = await _redis()
    if client is None:
        _LOCAL.setdefault(job_id, {})["_cancel"] = True
        return
    await client.set(_CANCEL + job_id, "1", ex=JOB_TTL_S)


async def cancel_requested(job_id: str) -> bool:
    client = await _redis()
    if client is None:
        return bool(_LOCAL.get(job_id, {}).get("_cancel"))
    return bool(await client.get(_CANCEL + job_id))


async def clear_cancel(job_id: str) -> None:
    client = await _redis()
    if client is None:
        _LOCAL.get(job_id, {}).pop("_cancel", None)
        return
    await client.delete(_CANCEL + job_id)


async def claim_connector(connector_id: str, job_id: str) -> str | None:
    """Claim a connector for this job, or return the job that already holds it.

    🚨 SET NX — one atomic operation, not get-then-set. Two pods handling two
    clicks in the same millisecond both read "nothing in flight" and both start
    a sync; the check and the claim have to be the same instruction or dedupe is
    a race with better odds.
    """
    client = await _redis()
    if client is None:
        holder = _LOCAL.get(_INFLIGHT + connector_id)
        if holder:
            return str(holder.get("job_id"))
        _LOCAL[_INFLIGHT + connector_id] = {"job_id": job_id}
        return None
    ok = await client.set(_INFLIGHT + connector_id, job_id, nx=True, ex=JOB_TTL_S)
    if ok:
        return None
    return await client.get(_INFLIGHT + connector_id)


async def release_connector(connector_id: str, job_id: str) -> None:
    """Release the claim — but only if it is still ours.

    A job that overran its TTL may have had its claim taken by a newer sync;
    deleting blindly would free a claim that now belongs to someone else.
    """
    client = await _redis()
    if client is None:
        holder = _LOCAL.get(_INFLIGHT + connector_id)
        if holder and holder.get("job_id") == job_id:
            _LOCAL.pop(_INFLIGHT + connector_id, None)
        return
    current = await client.get(_INFLIGHT + connector_id)
    if current == job_id:
        await client.delete(_INFLIGHT + connector_id)
