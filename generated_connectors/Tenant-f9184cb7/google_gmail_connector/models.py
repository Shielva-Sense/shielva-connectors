"""Pydantic request/response models for the Gmail connector."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class SendEmailRequest(BaseModel):
    to: str
    subject: str
    body: str
    cc: Optional[str] = None
    bcc: Optional[str] = None


class ModifyMessageRequest(BaseModel):
    message_id: str
    add_label_ids: List[str] = []
    remove_label_ids: List[str] = []


class ListEmailsRequest(BaseModel):
    query: str = ""
    max_results: int = 500
    page_token: Optional[str] = None


class GmailMessageStub(BaseModel):
    id: str
    threadId: Optional[str] = None


class ListEmailsResponse(BaseModel):
    messages: List[GmailMessageStub] = []
    nextPageToken: Optional[str] = None
    resultSizeEstimate: Optional[int] = None
