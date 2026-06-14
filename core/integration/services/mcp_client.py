"""Integration Builder — shielva-mcp LiteLLM client.

When INTEGRATION_LLM_MODE=mcp the code-generation calls are routed through
shielva-mcp's LiteLLM router instead of calling Claude CLI or the Anthropic
API directly.

Benefits:
  - One place to rotate API keys (MCP's .env, not integration's .env)
  - MCP's automatic model fallback chain (Gemini 2.0 → 2.5 → lite)
  - Future: RAG context injection for SDK docs / connector examples
  - Integration service needs zero LLM API keys of its own

Endpoint called:
  POST {INTEGRATION_MCP_URL}/mcp/v1/codegen/complete

Headers forwarded:
  X-Tenant-ID  — required by every MCP endpoint for tenant isolation
  X-User-ID    — "integration-builder" (internal service identity)
"""
from __future__ import annotations

import structlog
from typing import Any, Dict, List, Optional

import httpx

from integration.core.config import settings

logger = structlog.get_logger(__name__)


async def call_llm_via_mcp(
    messages: List[Dict[str, str]],
    *,
    system: str = "",
    tenant_id: str,
    model: Optional[str] = None,
    max_tokens: int = 8192,
    temperature: float = 0.3,
) -> str:
    """
    Send a code-generation request to shielva-mcp's LiteLLM router.

    Args:
        messages:    OpenAI-format message list (user/assistant turns).
        system:      System prompt string (injected before messages).
        tenant_id:   Tenant ID — forwarded as X-Tenant-ID header.
        model:       Optional model override (e.g. "gemini/gemini-2.5-pro").
                     When None, MCP uses its configured default model.
        max_tokens:  Maximum output tokens.
        temperature: Sampling temperature.

    Returns:
        Generated text string.

    Raises:
        RuntimeError: if MCP is unreachable or returns a non-2xx status.
    """
    url = f"{settings.MCP_URL}/mcp/v1/codegen/complete"

    payload: Dict[str, Any] = {
        "messages": messages,
        "system": system,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if model:
        payload["model"] = model

    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-ID": "integration-builder",
        "X-User-Email": "internal@shielva.ai",
        "Content-Type": "application/json",
    }

    logger.info(
        "mcp_client.call",
        tenant_id=tenant_id,
        mcp_url=url,
        msg_count=len(messages),
        system_length=len(system),
        max_tokens=max_tokens,
    )

    try:
        # 600 s timeout — large connector code-gen can take several minutes
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

    except httpx.HTTPStatusError as exc:
        body_preview = exc.response.text[:300]
        logger.error(
            "mcp_client.http_error",
            status=exc.response.status_code,
            body=body_preview,
            tenant_id=tenant_id,
        )
        raise RuntimeError(
            f"shielva-mcp codegen endpoint returned HTTP {exc.response.status_code}: "
            f"{body_preview}"
        ) from exc

    except httpx.RequestError as exc:
        logger.error(
            "mcp_client.connection_error",
            error=str(exc),
            url=url,
            tenant_id=tenant_id,
        )
        raise RuntimeError(
            f"Cannot reach shielva-mcp at {settings.MCP_URL}. "
            "Ensure MCP is running and INTEGRATION_MCP_URL is correct in .env. "
            f"Connection error: {exc}"
        ) from exc

    text: str = data.get("text", "")
    model_used: str = data.get("model", "unknown")
    tokens_used: int = data.get("tokens_used", 0)

    logger.info(
        "mcp_client.response",
        tenant_id=tenant_id,
        model=model_used,
        tokens_used=tokens_used,
        response_length=len(text),
    )

    return text


async def fix_code_via_mcp_agent(
    broken_code: str,
    error_output: str,
    *,
    tenant_id: str,
    connector_class: str = "",
    user_prompt: str = "",
    step_memory_summary: str = "",
    model: Optional[str] = None,
    max_tokens: int = 16384,
    temperature: float = 0.2,
) -> str:
    """
    Use MCP's tool-calling agent loop to fix broken connector/test code.

    Unlike call_llm_via_mcp (pure text completion), this endpoint gives the
    LLM access to MCP's codegen tools so it can:
      - Categorize the error type before attempting a fix
      - Check pytest structure / import paths via static analysis tools
      - Validate the fix is syntactically correct before returning

    Called by llm_client.call_llm_fix() when LLM_MODE=mcp.

    Returns:
        Fixed Python source code string.
    """
    url = f"{settings.MCP_URL}/mcp/v1/codegen/fix-agent"

    payload: Dict[str, Any] = {
        "broken_code": broken_code,
        "error_output": error_output,
        "connector_class": connector_class,
        "user_prompt": user_prompt,
        "step_memory_summary": step_memory_summary,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if model:
        payload["model"] = model

    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-ID": "integration-builder",
        "X-User-Email": "internal@shielva.ai",
        "Content-Type": "application/json",
    }

    logger.info(
        "mcp_client.fix_agent",
        tenant_id=tenant_id,
        connector_class=connector_class,
        broken_code_length=len(broken_code),
        error_preview=error_output[:100],
    )

    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        body_preview = exc.response.text[:300]
        logger.error("mcp_client.fix_agent_http_error", status=exc.response.status_code, body=body_preview)
        raise RuntimeError(
            f"shielva-mcp fix-agent returned HTTP {exc.response.status_code}: {body_preview}"
        ) from exc
    except httpx.RequestError as exc:
        logger.error("mcp_client.fix_agent_connection_error", error=str(exc))
        raise RuntimeError(
            f"Cannot reach shielva-mcp fix-agent at {settings.MCP_URL}. Error: {exc}"
        ) from exc

    fixed_code: str = data.get("fixed_code", "")
    tools_called: list = data.get("tools_called", [])
    model_used: str = data.get("model", "unknown")

    logger.info(
        "mcp_client.fix_agent_ok",
        tenant_id=tenant_id,
        model=model_used,
        tools_called=tools_called,
        fixed_code_length=len(fixed_code),
    )

    return fixed_code
