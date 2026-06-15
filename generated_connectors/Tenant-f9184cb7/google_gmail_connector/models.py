"""Typed models for Gmail connector request/response shapes."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MessageStub:
    """Lightweight reference returned by messages.list."""
    id: str
    thread_id: str = ""


@dataclass
class BulkDeleteResult:
    """Return value of GmailConnector.bulk_delete()."""
    deleted: int = 0
    failed: int = 0
    errors: List[str] = field(default_factory=list)
