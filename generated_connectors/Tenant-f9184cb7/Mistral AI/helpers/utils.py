"""Connector-level utilities — retry helper + payload builders.

The HTTP client already retries 429/5xx at the transport level. This helper
gives the connector layer a second optional retry budget for callers that
want a separate policy (e.g. fine-tuning create which is slow).
"""
import asyncio
import random
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

from exceptions import MistralError, MistralRateLimitError

T = TypeVar("T")


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 0.5,
) -> T:
    """Run an async callable with exponential backoff + jitter retry.

    Retries only `MistralRateLimitError` and 5xx `MistralError`. Other errors
    (auth, not-found, bad-request) propagate immediately.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries):
        try:
            return await fn()
        except MistralRateLimitError as exc:
            last_exc = exc
            if attempt + 1 >= max_retries:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt) + random.uniform(0, 0.25))
        except MistralError as exc:
            status = getattr(exc, "status_code", 0)
            if 500 <= status < 600:
                last_exc = exc
                if attempt + 1 >= max_retries:
                    raise
                await asyncio.sleep(
                    base_delay * (2 ** attempt) + random.uniform(0, 0.25)
                )
            else:
                raise
    if last_exc:
        raise last_exc
    raise MistralError("with_retry exhausted with no exception captured")


def build_chat_payload(
    model: str,
    messages: List[Dict[str, Any]],
    temperature: float = 0.7,
    max_tokens: int = 1024,
    top_p: float = 1.0,
    stream: bool = False,
    response_format: Optional[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a /chat/completions request body, omitting null optional fields."""
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": top_p,
        "stream": stream,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    if tools is not None:
        payload["tools"] = tools
    return payload


def build_embeddings_payload(
    model: str,
    inputs: List[str],
    encoding_format: str = "float",
) -> Dict[str, Any]:
    """Build a /embeddings request body.

    Note: Mistral wire key is `input` (singular), even though it's a list.
    """
    return {
        "model": model,
        "input": inputs,
        "encoding_format": encoding_format,
    }


def build_fine_tuning_payload(
    model: str,
    training_files: List[Any],
    hyperparameters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a /fine_tuning/jobs request body.

    `training_files` may be a list of file_id strings (wrapped) or already-built
    `{file_id: ...}` dicts.
    """
    payload: Dict[str, Any] = {
        "model": model,
        "training_files": [
            {"file_id": fid} if isinstance(fid, str) else fid for fid in training_files
        ],
    }
    if hyperparameters:
        payload["hyperparameters"] = hyperparameters
    return payload
