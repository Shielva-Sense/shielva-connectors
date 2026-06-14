"""Pydantic request/response models for the Gmail connector."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class GmailMessageHeader(BaseModel):
    name: str
    value: str


class GmailMessagePayload(BaseModel):
    headers: List[GmailMessageHeader] = Field(default_factory=list)


class GmailMessage(BaseModel):
    id: str
    threadId: str = ""
    snippet: str = ""
    labelIds: List[str] = Field(default_factory=list)
    payload: Optional[GmailMessagePayload] = None


class GmailListResponse(BaseModel):
    messages: List[Dict[str, str]] = Field(default_factory=list)
    nextPageToken: Optional[str] = None
    resultSizeEstimate: int = 0


class GmailProfile(BaseModel):
    emailAddress: str
    messagesTotal: int = 0
    threadsTotal: int = 0
    historyId: str = ""


class TokenExchangeResponse(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: int = 3600
    token_type: str = "Bearer"
    scope: str = ""
