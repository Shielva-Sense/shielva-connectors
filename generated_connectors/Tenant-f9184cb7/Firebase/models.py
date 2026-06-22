"""Pydantic request/response schemas for the Firebase REST surfaces.

camelCase aliases match the Google wire format; the connector boundary
accepts/returns `Dict[str, Any]` payloads — the models are used internally
by `FirebaseHTTPClient` to validate FCM + Auth bodies before they go out.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _FirebaseModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class FCMMessage(_FirebaseModel):
    """FCM v1 message body — passed inside `{"message": {...}}`.

    Exactly one of `token` / `topic` / `condition` is required by FCM; the
    connector enforces token/topic at the public surface, then this model
    serialises only the populated fields (everything else is dropped via
    `model_dump(exclude_none=True)`).
    """

    token: Optional[str] = None
    topic: Optional[str] = None
    condition: Optional[str] = None
    notification: Optional[Dict[str, Any]] = None
    data: Optional[Dict[str, str]] = None
    android: Optional[Dict[str, Any]] = None
    apns: Optional[Dict[str, Any]] = None
    webpush: Optional[Dict[str, Any]] = None

    def to_payload(self) -> Dict[str, Any]:
        message = self.model_dump(exclude_none=True)
        return {"message": message}


class CreateUserRequest(_FirebaseModel):
    """Body for POST https://identitytoolkit.googleapis.com/v1/accounts."""

    email: str
    password: Optional[str] = None
    display_name: Optional[str] = Field(default=None, alias="displayName")
    phone_number: Optional[str] = Field(default=None, alias="phoneNumber")
    photo_url: Optional[str] = Field(default=None, alias="photoUrl")
    email_verified: Optional[bool] = Field(default=None, alias="emailVerified")
    disabled: Optional[bool] = None


class UpdateUserRequest(_FirebaseModel):
    """Body for POST {identity_base}/accounts:update."""

    local_id: str = Field(alias="localId")
    email: Optional[str] = None
    password: Optional[str] = None
    display_name: Optional[str] = Field(default=None, alias="displayName")
    phone_number: Optional[str] = Field(default=None, alias="phoneNumber")
    photo_url: Optional[str] = Field(default=None, alias="photoUrl")
    email_verified: Optional[bool] = Field(default=None, alias="emailVerified")
    disabled: Optional[bool] = None


class ListUsersRequest(_FirebaseModel):
    """Body for POST {identity_base}/accounts:batchGet."""

    max_results: int = Field(default=1000, alias="maxResults")
    next_page_token: Optional[str] = Field(default=None, alias="nextPageToken")


class PageResult(_FirebaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_page_token: Optional[str] = Field(default=None, alias="nextPageToken")
