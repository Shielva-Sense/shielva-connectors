"""Bill.com connector exception hierarchy.

Bill.com surfaces errors in two layers:

  1. Transport / HTTP — `httpx.NetworkError`, `httpx.TimeoutException`, 5xx, 429.
  2. Envelope — `response_status=1` with an `error_code` + `error_message`.

The hierarchy below mirrors both layers so the connector layer can react
correctly: transient transport errors → retry; auth errors → operator fix;
session expiry → silent re-login.
"""

from typing import Optional


class BillcomError(Exception):
    """Base for all Bill.com connector errors.

    Carries:
      * `response_code` — the Bill.com `error_code` from the envelope (e.g. ``BDC_1018``).
      * `response_body` — the raw envelope dict (for telemetry / debugging).
      * `status_code` — HTTP status when the error is transport-level; 0 for
        envelope-level errors that arrived with HTTP 200.
    """

    def __init__(
        self,
        message: str = "",
        response_code: str = "",
        response_body: Optional[dict] = None,
        status_code: int = 0,
    ):
        super().__init__(message)
        self.response_code = response_code
        self.response_body = response_body or {}
        self.status_code = status_code


class BillcomAuthError(BillcomError):
    """Login refused — bad `dev_key`, `user_name`, `password`, or `org_id`.

    Codes seen in the wild: BDC_1011 (invalid devKey), BDC_1018 (invalid
    username/password), BDC_1019 (account locked), BDC_1020/BDC_1021 (org access
    denied). Should NOT be retried — operator must fix the bundle.
    """


class BillcomSessionExpired(BillcomError):
    """Cached `sessionId` was rejected by Bill.com.

    Codes seen: BDC_1024 ("Invalid Session"), or any error_message containing
    "Invalid Session" / "session expired". The connector layer clears the cache
    and silently re-logs-in — callers do NOT see this exception under normal use.
    """


class BillcomBadRequestError(BillcomError):
    """Malformed request body (HTTP 400 or envelope BDC_1xxx validation codes)."""


class BillcomNotFoundError(BillcomError):
    """Requested resource does not exist (HTTP 404 or envelope NotFound codes)."""


class BillcomConflictError(BillcomError):
    """Conflict / duplicate (HTTP 409 or envelope duplicate-key codes)."""


class BillcomRateLimitError(BillcomError):
    """Hit Bill.com's rate limit. `retry_after_s` is the suggested wait."""

    def __init__(self, message: str, retry_after_s: float = 5.0):
        super().__init__(message, status_code=429)
        self.retry_after_s = retry_after_s


class BillcomServerError(BillcomError):
    """5xx — provider outage; retry candidate."""


# ── Back-compat aliases (older callers/tests import these names) ───────────
BillcomNetworkError = BillcomServerError
BillcomNotFound = BillcomNotFoundError
