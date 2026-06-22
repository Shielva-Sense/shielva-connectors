"""Misc utility helpers for the ADP connector.

Owns:
  * `with_retry` — escape-hatch retry for unexpected transient errors that
    sneak past `client/http_client.py::_request`.
  * `materialize_pem` — projects an inline PEM string (the value of the
    `client_cert` / `client_key` install_field) to a tmp file so httpx can
    load it through its `cert=(crt_path, key_path)` API.
  * `build_time_off_event` / `build_email_change_event` — ADP Events API
    envelope builders.
  * `safe_get` — nested-dict accessor for normalizers.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff retry."""
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc
    raise RuntimeError("with_retry: exhausted retries without exception")


def safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict path safely."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def looks_like_pem(value: str, marker: str) -> bool:
    """Cheap shape check — does the string contain a PEM BEGIN marker?"""
    if not isinstance(value, str):
        return False
    return marker in value


def materialize_pem(
    pem_value: str,
    *,
    prefix: str,
    existing_path: Optional[str] = None,
) -> str:
    """Write PEM content to a tmp file (so httpx can load it via path).

    If `existing_path` is provided and points to a file whose content matches
    `pem_value`, the existing path is returned untouched. Otherwise a new tmp
    file is written and its path returned.

    The caller is responsible for cleaning up the file via `os.unlink` when the
    connector is uninstalled — installs in long-running services keep the file
    around for the connector's lifetime which mirrors what an operator running
    `ADP_CLIENT_CERT_PATH=/etc/.../client.crt` would have.
    """
    if existing_path and os.path.isfile(existing_path):
        try:
            with open(existing_path, "r", encoding="utf-8") as fh:
                if fh.read() == pem_value:
                    return existing_path
        except OSError:
            pass

    fd, path = tempfile.mkstemp(prefix=f"shielva-adp-{prefix}-", suffix=".pem")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(pem_value)
        os.chmod(path, 0o600)
    except OSError:
        # Best-effort: leave the descriptor closed, attempt cleanup and re-raise.
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def build_time_off_event(
    worker_aoid: str,
    policy_code: str,
    start_date: str,
    end_date: str,
    hours: Optional[float] = None,
    comments: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the body for POST /time-off/v2/workers/{aoid}/time-off-requests."""
    body: Dict[str, Any] = {
        "events": [
            {
                "data": {
                    "eventContext": {
                        "worker": {"associateOID": worker_aoid},
                    },
                    "transform": {
                        "timeOffRequest": {
                            "timeOffPolicyCode": {"codeValue": policy_code},
                            "startDate": start_date,
                            "endDate": end_date,
                        }
                    },
                }
            }
        ]
    }
    tor = body["events"][0]["data"]["transform"]["timeOffRequest"]
    if hours is not None:
        tor["totalTimeOffHours"] = hours
    if comments:
        tor["comments"] = [{"textValue": comments}]
    return body


def build_email_change_event(
    worker_aoid: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the body for POST /events/hr/v1/worker.business-communication.email.change.

    At least one of `email` / `phone` must be supplied.
    """
    if not email and not phone:
        raise ValueError("build_email_change_event requires at least one of email/phone")

    transform: Dict[str, Any] = {}
    if email:
        transform["businessCommunication"] = {"email": {"emailUri": email}}
    if phone:
        transform.setdefault("businessCommunication", {})
        transform["businessCommunication"]["landline"] = {"formattedNumber": phone}

    return {
        "events": [
            {
                "data": {
                    "eventContext": {
                        "worker": {"associateOID": worker_aoid},
                    },
                    "transform": transform,
                }
            }
        ]
    }
