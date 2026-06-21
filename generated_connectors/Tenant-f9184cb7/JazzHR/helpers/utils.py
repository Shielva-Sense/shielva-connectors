"""Shared utilities for the JazzHR connector."""
from typing import Any, List


def ensure_list(value: Any) -> List[Any]:
    """Coerce a JazzHR response into a list.

    JazzHR list endpoints return a JSON array. Single-record endpoints
    (`/jobs/{id}`, `/users/{id}`) return a one-element array too — but a few
    legacy endpoints return a bare object. This helper normalises both.
    `None` / `{}` collapse to `[]`.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and not value:
        return []
    return [value]
