"""Integration Builder — Redis-based LLM job queue.

Architecture:
  [Production FastAPI] → pushes job to Redis → [Worker machine with Claude CLI] → result back via Redis

Flow:
  1. Production server calls `enqueue_llm_job(prompt, system)` → returns job_id
  2. Worker machine runs `llm_worker.py` — polls Redis, calls `claude -p`, pushes result
  3. Production server calls `await wait_for_result(job_id)` — blocks until result ready

Redis keys:
  llm:jobs          — LIST (FIFO queue of job IDs)
  llm:job:{id}      — HASH with: prompt, system, status, result, error, created_at, completed_at
"""

import time
import uuid
from typing import Any

import redis.asyncio as aioredis
import structlog

from integration.core.config import settings

logger = structlog.get_logger(__name__)

_redis: aioredis.Redis | None = None

QUEUE_KEY = "llm:jobs"
JOB_PREFIX = "llm:job:"
JOB_TTL = 3600  # 1 hour TTL for completed jobs


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )
    return _redis


async def enqueue_llm_job(
    prompt: str,
    system: str = "",
    max_tokens: int | None = None,
) -> str:
    """Push an LLM job to the Redis queue. Returns job_id."""
    r = await _get_redis()
    job_id = str(uuid.uuid4())
    job_key = f"{JOB_PREFIX}{job_id}"

    job_data = {
        "prompt": prompt,
        "system": system,
        "max_tokens": str(max_tokens or settings.LLM_MAX_TOKENS),
        "status": "pending",
        "result": "",
        "error": "",
        "created_at": str(time.time()),
        "completed_at": "",
    }

    # Store job data + push to queue atomically via pipeline
    async with r.pipeline(transaction=True) as pipe:
        pipe.hset(job_key, mapping=job_data)
        pipe.expire(job_key, JOB_TTL)
        pipe.rpush(QUEUE_KEY, job_id)
        await pipe.execute()

    logger.info("llm_queue.enqueued", job_id=job_id, prompt_length=len(prompt))
    return job_id


async def wait_for_result(
    job_id: str,
    timeout: float = 300,  # 5 minutes
    poll_interval: float = 0.5,
) -> str:
    """Block until the worker completes the job. Returns the LLM response text.

    Raises TimeoutError or RuntimeError on failure.
    """
    r = await _get_redis()
    job_key = f"{JOB_PREFIX}{job_id}"
    deadline = time.time() + timeout

    while time.time() < deadline:
        job = await r.hgetall(job_key)
        if not job:
            raise RuntimeError(f"Job {job_id} not found in Redis")

        status = job.get("status", "")
        if status == "completed":
            logger.info("llm_queue.result_ready", job_id=job_id)
            return job.get("result", "")
        if status == "failed":
            error = job.get("error", "Unknown error")
            raise RuntimeError(f"LLM worker failed: {error}")

        # Still pending or processing — wait and retry
        import asyncio

        await asyncio.sleep(poll_interval)

    raise TimeoutError(f"LLM job {job_id} timed out after {timeout}s")


async def get_queue_stats() -> dict[str, Any]:
    """Get queue statistics (useful for health checks)."""
    r = await _get_redis()
    queue_length = await r.llen(QUEUE_KEY)
    return {
        "queue_length": queue_length,
        "redis_url": settings.REDIS_URL.split("@")[-1] if "@" in settings.REDIS_URL else settings.REDIS_URL,
    }
