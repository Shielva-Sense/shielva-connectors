"""AWS S3 connector exception hierarchy.

Mirrors the Wix / OpenAI gold-standard pattern: a base `AwsS3Error` that carries
`status_code` + `response_body`, plus three subclasses for the cases that
`connector.py::health_check()` and the retry helper need to distinguish.

`AwsS3NetworkError` is kept as a sibling of `AwsS3Error` for transport-level
failures (DNS, socket, TLS, `EndpointConnectionError`) so the retry helper can
treat it uniformly without parsing strings.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class AwsS3Error(Exception):
    """Base for all AWS S3 connector errors.

    `status_code` is the HTTP status when known (0 for transport-level errors).
    `response_body` carries the parsed AWS error envelope when available — kept
    for debug logs only; never surfaced to end-users.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        response_body: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class AwsS3AuthError(AwsS3Error):
    """401 / 403 — AccessDenied, InvalidAccessKeyId, SignatureDoesNotMatch, ExpiredToken."""


class AwsS3NotFound(AwsS3Error):
    """404 — NoSuchKey, NoSuchBucket."""


class AwsS3NetworkError(AwsS3Error):
    """Transport-level failures — DNS, socket, TLS, `EndpointConnectionError`."""


# Back-compat alias so callers using the longer name also work.
AwsS3NotFoundError = AwsS3NotFound
