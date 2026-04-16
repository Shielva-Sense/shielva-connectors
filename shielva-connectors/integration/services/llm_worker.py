#!/usr/bin/env python3
"""Integration Builder — Claude CLI Worker.

Run this on any machine that has `claude` CLI installed and logged in (your Mac, a
dedicated worker box, etc.). It polls Redis for LLM jobs and processes them using
the Claude CLI with your Max subscription.

Usage:
    # From project root (with venv activated):
    python -m integration.services.llm_worker

    # Or directly:
    REDIS_URL=redis://your-redis:6379 python integration/services/llm_worker.py

    # With custom Claude CLI path:
    INTEGRATION_CLAUDE_CLI_PATH=/usr/local/bin/claude python -m integration.services.llm_worker

Environment variables:
    INTEGRATION_REDIS_URL       — Redis connection URL (default: redis://localhost:6379)
    INTEGRATION_CLAUDE_CLI_PATH — Path to claude binary (default: claude)

The worker:
  1. Blocks on BLPOP (Redis list pop) waiting for jobs
  2. Calls `claude -p` with the prompt via stdin
  3. Writes result back to the job hash in Redis
  4. Loops forever — Ctrl+C to stop
"""

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time

import redis

# ── Configuration ────────────────────────────────────────────────────

REDIS_URL = os.getenv("INTEGRATION_REDIS_URL", "redis://localhost:6379")
CLAUDE_CLI = os.getenv("INTEGRATION_CLAUDE_CLI_PATH", "claude")
QUEUE_KEY = "llm:jobs"
JOB_PREFIX = "llm:job:"
POLL_TIMEOUT = 30  # seconds to block on BLPOP before re-checking


def main():
    print("=" * 60)
    print("  Shielva Integration Builder — Claude CLI Worker")
    print("=" * 60)

    # Pre-flight checks
    cli_path = shutil.which(CLAUDE_CLI)
    if not cli_path:
        print(f"\n  ERROR: Claude CLI not found at '{CLAUDE_CLI}'")
        print("  Install: npm install -g @anthropic-ai/claude-code")
        print("  Then run: claude   (to log in once)")
        sys.exit(1)

    print(f"  Claude CLI : {cli_path}")
    print(f"  Redis      : {REDIS_URL}")
    print(f"  Queue      : {QUEUE_KEY}")
    print("=" * 60)

    # Test Claude CLI auth
    print("\n  Testing Claude CLI authentication...")
    try:
        proc = subprocess.run(
            [CLAUDE_CLI, "-p", "--output-format", "text"],
            input="Say 'ok' and nothing else.",
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "CLAUDECODE": ""},  # unset to avoid nesting check
        )
        if proc.returncode == 0 and proc.stdout.strip():
            print(f"  Auth OK — Claude responded: {proc.stdout.strip()[:50]}")
        else:
            print(f"  WARNING: Claude CLI returned code {proc.returncode}")
            if proc.stderr:
                print(f"  stderr: {proc.stderr[:200]}")
            print("  You may need to run 'claude' interactively first to log in.")
            response = input("  Continue anyway? (y/n): ")
            if response.lower() != "y":
                sys.exit(1)
    except subprocess.TimeoutExpired:
        print("  WARNING: Claude CLI timed out during auth test")
        print("  The worker will still try to process jobs.")
    except Exception as e:
        print(f"  WARNING: Auth test failed: {e}")

    # Connect to Redis
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True)
        r.ping()
        print(f"\n  Redis connected OK")
    except Exception as e:
        print(f"\n  ERROR: Cannot connect to Redis: {e}")
        sys.exit(1)

    # Graceful shutdown
    running = True

    def _shutdown(sig, frame):
        nonlocal running
        print("\n\n  Shutting down worker...")
        running = False

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── Main loop ────────────────────────────────────────────────────
    print(f"\n  Worker ready — waiting for jobs...\n")
    jobs_processed = 0

    while running:
        try:
            # BLPOP blocks until a job appears (or timeout)
            result = r.blpop(QUEUE_KEY, timeout=POLL_TIMEOUT)
            if result is None:
                # Timeout — no jobs, loop back
                continue

            _, job_id = result
            job_key = f"{JOB_PREFIX}{job_id}"

            # Read job data
            job = r.hgetall(job_key)
            if not job:
                print(f"  WARN: Job {job_id} not found in Redis — skipping")
                continue

            prompt = job.get("prompt", "")
            system_prompt = job.get("system", "")
            status = job.get("status", "")

            if status != "pending":
                print(f"  SKIP: Job {job_id} status={status} — not pending")
                continue

            # Mark as processing
            r.hset(job_key, "status", "processing")

            print(f"  [{jobs_processed + 1}] Processing job {job_id[:8]}... (prompt: {len(prompt)} chars)")

            # Build full prompt with system context
            full_prompt = ""
            if system_prompt:
                full_prompt += f"<system>\n{system_prompt}\n</system>\n\n"
            full_prompt += prompt

            # Call Claude CLI
            start = time.time()
            try:
                proc = subprocess.run(
                    [CLAUDE_CLI, "-p", "--output-format", "text"],
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 min timeout
                    env={**os.environ, "CLAUDECODE": ""},  # avoid nesting block
                )

                duration = round(time.time() - start, 1)

                if proc.returncode == 0:
                    response_text = proc.stdout.strip()
                    r.hset(job_key, mapping={
                        "status": "completed",
                        "result": response_text,
                        "completed_at": str(time.time()),
                    })
                    r.expire(job_key, 3600)  # 1h TTL
                    jobs_processed += 1
                    print(f"        Done in {duration}s — response: {len(response_text)} chars")
                else:
                    error_msg = proc.stderr[:500] if proc.stderr else f"Exit code {proc.returncode}"
                    r.hset(job_key, mapping={
                        "status": "failed",
                        "error": error_msg,
                        "completed_at": str(time.time()),
                    })
                    r.expire(job_key, 3600)
                    print(f"        FAILED in {duration}s — {error_msg[:100]}")

            except subprocess.TimeoutExpired:
                r.hset(job_key, mapping={
                    "status": "failed",
                    "error": "Claude CLI timed out after 300s",
                    "completed_at": str(time.time()),
                })
                r.expire(job_key, 3600)
                print(f"        TIMEOUT after 300s")

            except Exception as exc:
                r.hset(job_key, mapping={
                    "status": "failed",
                    "error": str(exc)[:500],
                    "completed_at": str(time.time()),
                })
                r.expire(job_key, 3600)
                print(f"        ERROR: {exc}")

        except redis.ConnectionError:
            print("  Redis connection lost — reconnecting in 5s...")
            time.sleep(5)
            try:
                r = redis.from_url(REDIS_URL, decode_responses=True)
                r.ping()
                print("  Reconnected to Redis")
            except Exception:
                pass

        except Exception as exc:
            print(f"  Unexpected error: {exc}")
            time.sleep(1)

    print(f"\n  Worker stopped. Processed {jobs_processed} jobs total.")


if __name__ == "__main__":
    main()
