"""Integration Builder — Claude Models API route.

Returns the list of available Claude models from the Anthropic API.
Used by the Builder Advanced frontend to populate the model selector dynamically.
"""

import structlog
from fastapi import APIRouter

from integration.core.config import settings

logger = structlog.get_logger(__name__)

models_router = APIRouter(prefix="/models", tags=["models"])


@models_router.get("")
async def list_claude_models():
    """Return available Claude models from the Anthropic API.

    Falls back to a hardcoded list if the API key is not configured or the
    Anthropic API call fails, so the frontend always gets a usable response.
    """
    _FALLBACK = [
        {"id": "claude-opus-4-5", "display_name": "Claude Opus 4.5"},
        {"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"},
        {"id": "claude-haiku-3-5", "display_name": "Claude Haiku 3.5"},
        {"id": "claude-opus-4-0", "display_name": "Claude Opus 4"},
        {"id": "claude-sonnet-3-7", "display_name": "Claude Sonnet 3.7"},
    ]

    if not settings.ANTHROPIC_API_KEY:
        logger.warning("models.no_api_key — returning fallback list")
        return {"models": _FALLBACK, "source": "fallback"}

    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = await client.models.list()

        models = []
        for m in response.data:
            # Only surface Claude models (filter out any non-Claude entries)
            if "claude" in m.id.lower():
                display = getattr(m, "display_name", None) or _prettify(m.id)
                models.append({"id": m.id, "display_name": display})

        if not models:
            return {"models": _FALLBACK, "source": "fallback_empty"}

        return {"models": models, "source": "anthropic_api"}

    except Exception as exc:
        logger.warning("models.fetch_failed — returning fallback", error=str(exc))
        return {"models": _FALLBACK, "source": "fallback_error"}


def _prettify(model_id: str) -> str:
    """Convert e.g. 'claude-opus-4-5' → 'Claude Opus 4.5'"""
    parts = model_id.replace("claude-", "").split("-")
    result = []
    for p in parts:
        if p.isdigit() or (len(p) <= 2 and any(c.isdigit() for c in p)):
            result.append(p)
        else:
            result.append(p.capitalize())
    return "Claude " + " ".join(result)
