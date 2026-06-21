"""Pydantic request/response schemas for the Anthropic REST API.

The Anthropic wire format is snake_case (`stop_reason`, `input_tokens`,
`output_tokens`) so no alias gymnastics are required. The connector
boundary itself uses ``Dict[str, Any]`` payloads — these models are kept
as documentation + optional caller-side validation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _AnthropicModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Message(_AnthropicModel):
    role: str  # "user" | "assistant"
    content: Any  # string or list of content blocks


class CreateMessageRequest(_AnthropicModel):
    model: str
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    max_tokens: int = 1024
    system: Optional[str] = None
    temperature: float = 1.0
    stream: bool = False


class Usage(_AnthropicModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None


class MessageResponse(_AnthropicModel):
    id: str
    type: str = "message"
    role: str = "assistant"
    model: str
    content: List[Dict[str, Any]] = Field(default_factory=list)
    stop_reason: Optional[str] = None
    stop_sequence: Optional[str] = None
    usage: Optional[Usage] = None


class ModelInfo(_AnthropicModel):
    id: str
    type: str = "model"
    display_name: Optional[str] = None
    created_at: Optional[str] = None


class ModelListResponse(_AnthropicModel):
    data: List[ModelInfo] = Field(default_factory=list)
    has_more: bool = False
    first_id: Optional[str] = None
    last_id: Optional[str] = None


class BatchRequest(_AnthropicModel):
    custom_id: str
    params: Dict[str, Any]


class BatchInfo(_AnthropicModel):
    id: str
    type: str = "message_batch"
    processing_status: str = "in_progress"
    request_counts: Dict[str, int] = Field(default_factory=dict)
    created_at: Optional[str] = None
    expires_at: Optional[str] = None


class FileInfo(_AnthropicModel):
    id: str
    type: str = "file"
    filename: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    created_at: Optional[str] = None


class PageResult(_AnthropicModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    has_more: bool = False
    last_id: Optional[str] = None
