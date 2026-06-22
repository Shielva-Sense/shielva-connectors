"""Pydantic request/response schemas for the Cohere REST API.

Cohere wire format is snake_case, so we keep field names as-is. These models
are used for type-safety inside the connector boundary; the public method
signatures remain `Dict[str, Any]` to mirror Wix/Bandwidth conventions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _CohereModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


# ── Requests ────────────────────────────────────────────────────────────────


class ChatMessage(_CohereModel):
    """One turn in a chat conversation."""

    role: str
    content: Any  # may be a string or a structured content-block list


class ChatRequest(_CohereModel):
    model: str
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    temperature: float = 0.3
    max_tokens: int = 1024
    stream: bool = False
    p: Optional[float] = None
    k: Optional[int] = None
    stop_sequences: List[str] = Field(default_factory=list)


class EmbedRequest(_CohereModel):
    model: str
    texts: List[str] = Field(default_factory=list)
    input_type: str = "search_document"
    embedding_types: List[str] = Field(default_factory=lambda: ["float"])
    truncate: str = "END"


class RerankRequest(_CohereModel):
    model: str
    query: str
    documents: List[Any] = Field(default_factory=list)
    top_n: int = 10
    return_documents: bool = False


class ClassifyRequest(_CohereModel):
    model: str
    inputs: List[str] = Field(default_factory=list)
    examples: List[Dict[str, str]] = Field(default_factory=list)


class TokenizeRequest(_CohereModel):
    model: str
    text: str


class DetokenizeRequest(_CohereModel):
    model: str
    tokens: List[int] = Field(default_factory=list)


# ── Responses ───────────────────────────────────────────────────────────────


class ModelInfo(_CohereModel):
    name: str = ""
    endpoints: List[str] = Field(default_factory=list)
    finetuned: bool = False
    context_length: Optional[int] = None
    default_endpoints: List[str] = Field(default_factory=list)


class DatasetInfo(_CohereModel):
    dataset_id: str = Field(default="", alias="id")
    name: Optional[str] = None
    dataset_type: Optional[str] = None
    validation_status: Optional[str] = None
    size: Optional[int] = None
    created_at: Optional[str] = None


class PageResult(_CohereModel):
    """Generic Cohere paginated list response."""

    items: List[Dict[str, Any]] = Field(default_factory=list)
    next_page_token: Optional[str] = None
