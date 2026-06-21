"""Pydantic request/response schemas for Dropbox API v2.

The connector boundary uses ``Dict[str, Any]`` payloads (consistent with the
rest of the Shielva connector estate). These models exist so callers that want
typed access to a response can opt in via ``ListFolderResponse.model_validate(...)``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _DropboxModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class FileEntry(_DropboxModel):
    """A single ``.tag == 'file'`` entry from list_folder / get_metadata."""

    tag: str = Field(default="file", alias=".tag")
    id: Optional[str] = None
    name: str = ""
    path_lower: Optional[str] = None
    path_display: Optional[str] = None
    client_modified: Optional[datetime] = None
    server_modified: Optional[datetime] = None
    rev: Optional[str] = None
    size: int = 0
    is_downloadable: bool = True
    content_hash: Optional[str] = None


class FolderEntry(_DropboxModel):
    """A single ``.tag == 'folder'`` entry."""

    tag: str = Field(default="folder", alias=".tag")
    id: Optional[str] = None
    name: str = ""
    path_lower: Optional[str] = None
    path_display: Optional[str] = None


class ListFolderResponse(_DropboxModel):
    entries: List[Dict[str, Any]] = Field(default_factory=list)
    cursor: Optional[str] = None
    has_more: bool = False


class AccountInfo(_DropboxModel):
    account_id: Optional[str] = None
    name: Optional[Dict[str, Any]] = None
    email: Optional[str] = None
    email_verified: Optional[bool] = None
    country: Optional[str] = None
    account_type: Optional[Dict[str, Any]] = None


class SpaceUsage(_DropboxModel):
    used: int = 0
    allocation: Optional[Dict[str, Any]] = None


class SharedLinkRequest(_DropboxModel):
    path: str
    settings: Optional[Dict[str, Any]] = None


class SearchOptions(_DropboxModel):
    path: Optional[str] = None
    max_results: int = 100
    file_status: str = "active"


class SearchRequest(_DropboxModel):
    query: str
    options: SearchOptions = Field(default_factory=SearchOptions)
