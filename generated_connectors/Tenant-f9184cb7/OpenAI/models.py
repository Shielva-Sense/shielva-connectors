"""Lightweight result models for the OpenAI connector.

The canonical `ConnectorStatus`, `SyncResult`, and `NormalizedDocument`
come from `shared.base_connector` and are imported directly in
`connector.py`. This module hosts only connector-private shapes (e.g.
parsed chat-completion response) that aren't part of the gateway contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ChatCompletionResult:
    """A flattened view of a `/v1/chat/completions` response.

    `raw` carries the full OpenAI envelope for callers that need anything
    not surfaced through the flat fields.
    """

    text: str
    model: str
    finish_reason: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EmbeddingResult:
    """A flattened view of a `/v1/embeddings` response."""

    vector: List[float]
    model: str
    prompt_tokens: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)
