"""Helpers package — response normalisers + small utilities."""

from .normalizer import normalize_chat_completion
from .utils import extract_embedding_vector, normalize_chat_response

__all__ = [
    "extract_embedding_vector",
    "normalize_chat_completion",
    "normalize_chat_response",
]
