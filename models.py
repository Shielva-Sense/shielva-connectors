"""
Gmail Connector — Pydantic Request/Response Models
These models validate inputs at the connector boundary.
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, field_validator


class SendEmailRequest(BaseModel):
    """Validated request model for send_email()."""
    to: str
    subject: str
    body: str
    cc: Optional[str] = None
    bcc: Optional[str] = None
    reply_to: Optional[str] = None
    attachments: Optional[List[Dict[str, Any]]] = None


class ListEmailsRequest(BaseModel):
    """Validated request model for list_emails() and search_email()."""
    page_token: Optional[str] = None
    max_results: int = 20
    query: Optional[str] = None

    @field_validator("max_results")
    @classmethod
    def clamp_max_results(cls, v: int) -> int:
        if v < 1:
            return 1
        if v > 500:
            return 500
        return v


class DeleteEmailRequest(BaseModel):
    """Validated request model for delete_email()."""
    message_id: str
    permanent: bool = False
