"""Shared utilities for the OpenAI connector — response shaping helpers."""

from __future__ import annotations

from typing import Any, Dict


def normalize_chat_response(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Pick the salient fields out of a raw ``/v1/chat/completions`` response.

    Returns a dict with ``id``, ``model``, ``content`` (first-choice assistant
    text), ``finish_reason``, ``usage`` (prompt/completion/total tokens), and
    ``raw`` (the input dict, unchanged).
    """
    choices = raw.get("choices") or []
    first_choice: Dict[str, Any] = choices[0] if choices else {}
    message: Dict[str, Any] = first_choice.get("message") or {}
    usage: Dict[str, Any] = raw.get("usage") or {}

    return {
        "id": raw.get("id", ""),
        "model": raw.get("model", ""),
        "content": message.get("content", "") or "",
        "role": message.get("role", "assistant"),
        "finish_reason": first_choice.get("finish_reason", ""),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "raw": raw,
    }


def extract_embedding_vector(raw: Dict[str, Any]) -> list:
    """Return the first embedding vector from a `/v1/embeddings` response."""
    data = raw.get("data") or []
    if not data:
        return []
    first = data[0]
    if not isinstance(first, dict):
        return []
    vec = first.get("embedding") or []
    return list(vec)
