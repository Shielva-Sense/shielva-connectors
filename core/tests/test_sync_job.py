"""A sync you can start is a sync you can ask about.

🚨 POST /connectors/{id}/sync generated a job_id, returned it, and wrote it
nowhere. There was no endpoint to poll and no record to poll — so the only way
to run a sync and see its outcome was the console's inline path, which the 30s
request budget exists to kill. An id you cannot poll is decoration.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

_SRC = (_CORE / "gateway.py").read_text()
_TREE = ast.parse(_SRC)


def _body(name: str) -> str:
    fn = next(n for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return "\n".join(_SRC.splitlines()[fn.lineno - 1 : fn.end_lineno])


def test_the_sync_job_is_recorded_when_the_id_is_handed_out() -> None:
    body = _body("sync_connector")
    assert "_job(job_id)" in body, "the returned job_id must correspond to a real record"
    assert '"kind": "sync"' in body


def test_every_outcome_reaches_the_record() -> None:
    """Success, a connector-reported failure and a crash are three different
    endings, and a job stuck on `running` forever is indistinguishable from a
    sync that is still going."""
    # Parsed, not grepped: the formatter splits a long call across lines, and a
    # substring assertion would then pass or fail on line width.
    body = _body("sync_connector")
    tree = ast.parse(textwrap.dedent(body))
    finishes = [
        n.args[0].value
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "_finish"
        and n.args
        and isinstance(n.args[0], ast.Constant)
    ]
    assert "completed" in finishes
    assert finishes.count("failed") >= 2, (
        "a connector-reported failure and a crash are different endings, and a "
        "job stuck on `running` is indistinguishable from one still going"
    )


def test_there_is_an_endpoint_to_poll() -> None:
    assert '@app.get("/connectors/{connector_id}/sync/jobs/{job_id}")' in _SRC


def test_a_job_id_alone_is_not_a_handle_on_someone_elses_sync() -> None:
    body = _body("sync_job_status")
    assert "_resolve_for_tenant(connector_id, tenant_id)" in body
    assert 'job.get("kind") != "sync"' in body, (
        "a deploy job id must not be readable through the sync endpoint — they share one store"
    )


def test_the_store_is_shared_not_duplicated() -> None:
    """A second store would have meant a second GC, a second TTL and a second
    status endpoint to drift from this one."""
    assert "_DEPLOY_JOBS" not in _SRC, "the store is named for what it holds, not its first user"
    assert "_JOBS: dict[str, dict[str, Any]] = {}" in _SRC


def test_sync_is_not_bound_by_the_request_budget() -> None:
    from services.wheel_version import is_newer  # noqa: F401  (module import sanity)

    assert "CONNECTOR_SYNC_TIMEOUT_S" in _SRC
    assert "LONG_RUNNING_METHODS" in _SRC


def test_a_second_click_returns_the_running_job() -> None:
    """🚨 Two syncs of one connector into one knowledge base race each other and
    double the upstream calls for no extra data. Clicking twice is impatience,
    not a request for two syncs."""
    body = _body("sync_connector")
    assert "already in progress" in body
    # 🚨 Claimed with SET NX in Redis, not read-then-write in a dict. Two pods
    # handling two clicks in the same millisecond both read "nothing in flight"
    # and both start a sync; the check and the claim must be one instruction.
    assert "job_store.claim_connector(" in body


def test_the_job_store_is_shared_across_replicas() -> None:
    """A dict is correct at one replica and a lie at two: a poll load-balanced
    elsewhere answers "not found", a cancel reaches a pod not running the job,
    and dedupe stops working — none of which shows up until the replica count
    changes."""
    for endpoint in ("sync_job_status", "cancel_sync_job"):
        assert "job_store.get(job_id)" in _body(endpoint), (
            f"{endpoint} must read the shared store, not this process's dict"
        )


def test_a_cancel_from_another_pod_is_actually_honoured() -> None:
    """Cancellation crosses pods as a flag, and a flag nobody reads is a label.
    Only the process holding a task can interrupt it."""
    assert "job_store.request_cancel(" in _body("cancel_sync_job")
    worker = _body("_sync_worker")
    assert "job_store.cancel_requested(job_id)" in worker, "queued jobs must check before starting"
    assert "_watch_for_cancel(" in worker, "running jobs need the flag watched"


def test_sync_is_queued_not_fired() -> None:
    """BackgroundTasks starts the coroutine immediately, however many are already
    running — which is exactly what a bounded pool exists to prevent."""
    body = _body("sync_connector")
    assert "_SYNC_QUEUE.put_nowait" in body
    assert "background_tasks.add_task" not in body
    assert "QueueFull" in body, "a full queue must refuse, not grow without limit"


def test_a_sync_can_be_cancelled_queued_or_running() -> None:
    body = _body("cancel_sync_job")
    assert "task.cancel()" in body, "a running sync must actually be interrupted"
    assert '"cancelled"' in body, "a queued sync must be stopped before it starts"


def test_cancelling_one_job_does_not_stop_the_pool() -> None:
    """CancelledError propagating out of the worker loop would take the worker
    with it, and one cancel would quietly halve the pool."""
    body = _body("_sync_worker")
    assert "except asyncio.CancelledError" in body
    assert "_SYNC_INFLIGHT.pop" in body, "a finished job must release its connector"


def test_the_blocking_executor_is_bounded_and_chosen() -> None:
    """🚨 run_in_executor(None, …) sizes itself from os.cpu_count(), which inside
    a container reports the NODE's cpus — 6 here, against a cgroup limit of 1.0
    — so the pool was sized from a number unrelated to what this pod may use."""
    assert "loop.run_in_executor(None, lambda: method(**params))" not in _SRC
    assert "_blocking_executor()" in _SRC
    assert "CONNECTOR_EXECUTOR_WORKERS" in _SRC
