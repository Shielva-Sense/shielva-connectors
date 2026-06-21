"""Pydantic request/response schemas for the Mistral REST API.

Mistral uses snake_case on the wire; these schemas mirror that exactly so the
connector boundary can accept `Dict[str, Any]` payloads without translation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class _MistralModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


# ── Chat completions ──────────────────────────────────────────────────────────


class ChatMessage(_MistralModel):
    role: str
    content: str


class ChatCompletionRequest(_MistralModel):
    model: str
    messages: List[ChatMessage]
    temperature: float = 0.7
    max_tokens: int = 1024
    top_p: float = 1.0
    stream: bool = False
    response_format: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None


class ChatCompletionChoice(_MistralModel):
    index: int
    message: Dict[str, Any]
    finish_reason: Optional[str] = None


class ChatCompletionResponse(_MistralModel):
    id: str
    object: str = "chat.completion"
    created: int = 0
    model: str = ""
    choices: List[ChatCompletionChoice] = Field(default_factory=list)
    usage: Optional[Dict[str, Any]] = None


# ── Embeddings ────────────────────────────────────────────────────────────────


class EmbeddingsRequest(_MistralModel):
    model: str
    input: List[str]
    encoding_format: str = "float"


class EmbeddingItem(_MistralModel):
    index: int
    embedding: List[float] = Field(default_factory=list)


class EmbeddingsResponse(_MistralModel):
    object: str = "list"
    data: List[EmbeddingItem] = Field(default_factory=list)
    model: str = ""
    usage: Optional[Dict[str, Any]] = None


# ── Models ────────────────────────────────────────────────────────────────────


class ModelInfo(_MistralModel):
    id: str
    object: str = "model"
    created: Optional[int] = None
    owned_by: Optional[str] = None
    max_context_length: Optional[int] = None
    capabilities: Optional[Dict[str, Any]] = None


class ModelList(_MistralModel):
    object: str = "list"
    data: List[ModelInfo] = Field(default_factory=list)


# ── Files ─────────────────────────────────────────────────────────────────────


class FileInfo(_MistralModel):
    id: str
    object: str = "file"
    bytes: int = 0
    created_at: int = 0
    filename: str = ""
    purpose: str = ""


class FileList(_MistralModel):
    object: str = "list"
    data: List[FileInfo] = Field(default_factory=list)
    total: int = 0


# ── Fine-tuning ───────────────────────────────────────────────────────────────


class FineTuningJobRequest(_MistralModel):
    model: str
    training_files: List[Dict[str, Any]]
    hyperparameters: Optional[Dict[str, Any]] = None


class FineTuningJob(_MistralModel):
    id: str
    object: str = "fine_tuning.job"
    model: str = ""
    status: str = ""
    created_at: Optional[int] = None
    fine_tuned_model: Optional[str] = None
    hyperparameters: Optional[Dict[str, Any]] = None
    training_files: List[Dict[str, Any]] = Field(default_factory=list)


class FineTuningJobList(_MistralModel):
    object: str = "list"
    data: List[FineTuningJob] = Field(default_factory=list)
    total: int = 0
