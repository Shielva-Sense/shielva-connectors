"""Integration Builder — Claude LLM client.

Supports three modes (set via INTEGRATION_LLM_MODE env var):
  - "cli"    → Shells out to `claude` CLI locally (dev, Max plan, $0)
  - "worker" → Pushes job to Redis queue, worker machine runs Claude CLI ($0)
  - "api"    → Uses Anthropic SDK directly (requires ANTHROPIC_API_KEY, $$)

Default: "cli" for local dev. Use "worker" for production.
"""

import asyncio
import contextlib
import json
import os
import shutil
from contextvars import ContextVar
from typing import Any

import structlog

from integration.core.config import settings

logger = structlog.get_logger(__name__)

# ── Tenant context propagation ────────────────────────────────────────
# When LLM_MODE=mcp every call must carry a tenant_id for the X-Tenant-ID
# header.  Rather than threading it through every call site in step_executor.py
# we store it in an async-safe ContextVar.  codegen_service.py sets it once
# per execution and all downstream call_llm* calls read it automatically.
_TENANT_ID_CTX: ContextVar[str] = ContextVar("_llm_tenant_id", default="")
_MODEL_CTX: ContextVar[str] = ContextVar("_llm_model", default="")


def set_llm_tenant_id(tenant_id: str) -> None:
    """Set the tenant_id for the current async task.

    Call this once at the start of each execution in codegen_service.py:
        set_llm_tenant_id(session.tenant_id)

    All subsequent call_llm* calls in the same async task will automatically
    include the tenant_id when routing via MCP.
    """
    _TENANT_ID_CTX.set(tenant_id)


def get_llm_tenant_id() -> str:
    """Return the tenant_id set for the current async task (or '' if not set)."""
    return _TENANT_ID_CTX.get("")


def set_llm_model(model: str) -> None:
    """Set the LLM model override for the current async task.

    Call this once at the start of each execution in codegen_service.py:
        set_llm_model(session.get("llm_model", "") or "")

    When non-empty, call_llm* will use this model instead of the default
    from settings.LLM_MODEL.
    """
    _MODEL_CTX.set(model)


def get_llm_model() -> str:
    """Return the model override set for the current async task (or '' for default)."""
    return _MODEL_CTX.get("")


# Semaphore: allow at most 1 concurrent Claude CLI call.
# Running multiple Claude CLI processes simultaneously on the same Max account
# triggers silent rate-limiting where stdout is empty (output_tokens > 0 but result="").
_CLI_SEMAPHORE = asyncio.Semaphore(1)

# ── CLI mode ─────────────────────────────────────────────────────────


async def _call_cli(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    max_tokens: int | None = None,
) -> str:
    """Call Claude via the CLI using `claude -p` (non-interactive pipe mode).

    Uses your Max subscription — no API key needed.
    """
    # Build the prompt from conversation messages only (system is passed via --system-prompt flag)
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            parts.append(content)
        elif role == "assistant":
            parts.append(f"[Previous assistant response]: {content}")

    full_prompt = "\n\n".join(parts)

    cli_path = settings.CLAUDE_CLI_PATH
    if not shutil.which(cli_path):
        raise RuntimeError(
            f"Claude CLI not found at '{cli_path}'. "
            "Install: npm install -g @anthropic-ai/claude-code  "
            "Or switch to API mode: set INTEGRATION_LLM_MODE=api"
        )

    # Build the final stdin payload.
    # When system is present we embed it as a clearly-marked block at the top
    # of stdin so we don't rely on --system-prompt (which silently produces
    # empty stdout when the text is large) or --allowedTools "" (an invalid
    # empty-string argument that causes the CLI to exit with no output).
    if system:
        stdin_payload = (
            "=== SYSTEM INSTRUCTIONS (follow exactly) ===\n"
            + system
            + "\n=== END SYSTEM INSTRUCTIONS ===\n\n"
            + full_prompt
        )
    else:
        stdin_payload = full_prompt

    # --model  → explicitly use the configured model (LLM_MODEL env var)
    # -p       → non-interactive pipe mode (reads stdin, writes stdout)
    # --output-format text → plain text output (no ANSI codes / JSON wrapper)
    # No --allowedTools flag: passing "" is invalid and causes silent empty output.
    # No --system-prompt flag: passing large text via a CLI arg causes silent empty output.
    cmd = [cli_path, "-p", "--output-format", "text", "--model", settings.LLM_MODEL]

    logger.info(
        "llm.cli_call",
        prompt_length=len(stdin_payload),
        system_length=len(system),
        model=settings.LLM_MODEL,
    )

    # Allow Claude CLI to run even inside an existing Claude Code session.
    # Newer Claude Code versions check CLAUDECODE env var and block nesting;
    # clearing it (alongside the legacy CLAUDE_CODE_ALLOW_NESTED flag) bypasses both checks.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["CLAUDE_CODE_ALLOW_NESTED"] = "1"

    async def _run_once(backoff: float = 0.0) -> str:
        """Run the Claude CLI once and return the text output."""
        if backoff:
            await asyncio.sleep(backoff)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin_payload.encode("utf-8")),
            timeout=900,  # 15 min timeout — large connector + 60+ failing tests can need >10 min
        )
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace")[:500]
            logger.error("llm.cli_error", returncode=proc.returncode, stderr=err_msg)
            raise RuntimeError(f"Claude CLI exited with code {proc.returncode}: {err_msg}")
        return stdout.decode("utf-8", errors="replace").strip()

    # Retry loop: Claude CLI sometimes returns empty when called concurrently or
    # when rate-limited (it exits 0 but produces no stdout).  We retry once
    # with a short back-off (5 s) using the semaphore to ensure only one CLI
    # process runs at a time.  After 2 failures, return empty so call_llm_json
    # can immediately fall back to Gemini instead of waiting minutes.
    _EMPTY_BACKOFFS = [0, 5]
    text = ""
    for _attempt, _backoff in enumerate(_EMPTY_BACKOFFS, start=1):
        async with _CLI_SEMAPHORE:
            text = await _run_once(backoff=_backoff)
        if text:
            break
        logger.warning(
            "llm.cli_empty_retrying",
            attempt=_attempt,
            next_backoff=_EMPTY_BACKOFFS[_attempt] if _attempt < len(_EMPTY_BACKOFFS) else None,
        )

    logger.info("llm.cli_response", response_length=len(text))
    return text


async def _call_cli_streaming(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    max_tokens: int | None = None,
    on_chunk=None,
) -> str:
    """Call Claude CLI and stream stdout chunks via on_chunk callback.

    on_chunk(text_so_far, chunk) is called as each chunk arrives.
    Returns the final full text.
    """
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            parts.append(content)
        elif role == "assistant":
            parts.append(f"[Previous assistant response]: {content}")
    full_prompt = "\n\n".join(parts)

    cli_path = settings.CLAUDE_CLI_PATH
    if not shutil.which(cli_path):
        raise RuntimeError(f"Claude CLI not found at '{cli_path}'.")

    # Same stdin-embedding approach as _call_cli — avoids the empty-output bug
    # caused by --allowedTools "" (invalid arg) and --system-prompt with large text.
    if system:
        stdin_payload = (
            "=== SYSTEM INSTRUCTIONS (follow exactly) ===\n"
            + system
            + "\n=== END SYSTEM INSTRUCTIONS ===\n\n"
            + full_prompt
        )
    else:
        stdin_payload = full_prompt

    cmd = [cli_path, "-p", "--output-format", "text", "--model", settings.LLM_MODEL]

    logger.info(
        "llm.cli_stream_call",
        prompt_length=len(stdin_payload),
        model=settings.LLM_MODEL,
    )

    # Allow Claude CLI to run even inside an existing Claude Code session.
    # Clear CLAUDECODE so newer Claude Code versions don't block the nested call.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["CLAUDE_CODE_ALLOW_NESTED"] = "1"

    # Use semaphore so only ONE Claude CLI process runs at a time — prevents
    # the silent empty-response issue caused by concurrent CLI invocations.
    async with _CLI_SEMAPHORE:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Send input and close stdin
        proc.stdin.write(stdin_payload.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        # Read stdout in chunks — stream every chunk for real-time feedback
        collected = []
        while True:
            chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=900)
            if not chunk:
                break
            text_chunk = chunk.decode("utf-8", errors="replace")
            collected.append(text_chunk)
            full_so_far = "".join(collected)

            if on_chunk:
                with contextlib.suppress(Exception):
                    await on_chunk(full_so_far, text_chunk)

        await proc.wait()
        full_text = "".join(collected).strip()

    if proc.returncode != 0:
        stderr_text = ""
        if proc.stderr:
            stderr_bytes = await proc.stderr.read()
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Claude CLI exited with code {proc.returncode}: {stderr_text}")

    logger.info("llm.cli_stream_response", response_length=len(full_text))
    return full_text


# ── Kimi mode (Moonshot AI — OpenAI-compatible) ───────────────────────


def _kimi_provider_name() -> str:
    """Derive a human-readable provider name from KIMI_BASE_URL at runtime."""
    base = settings.KIMI_BASE_URL.lower()
    if "deepseek" in base:
        return "deepseek"
    if "moonshot" in base or "kimi" in base:
        return "kimi"
    # Generic fallback: strip scheme and take the first hostname segment
    import re

    m = re.search(r"://([^/]+)", base)
    return m.group(1).split(".")[0] if m else "openai-compat"


async def _call_kimi(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.3,
) -> str:
    """Call any OpenAI-compatible LLM endpoint (DeepSeek, Kimi, etc.).

    Provider is determined at runtime from KIMI_BASE_URL — no code changes
    needed when switching between providers.
    Requires INTEGRATION_KIMI_API_KEY to be set in .env.
    """
    import httpx

    provider = _kimi_provider_name()

    if not settings.KIMI_API_KEY:
        raise RuntimeError(
            f"INTEGRATION_KIMI_API_KEY is not set — required for provider '{provider}'. Add it to integration/.env"
        )

    model = model or settings.KIMI_MODEL
    max_tokens = max_tokens or 8192

    # Build message list: system first, then user/assistant turns
    msgs: list[dict[str, str]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)

    payload = {
        "model": model,
        "messages": msgs,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    logger.info(
        "llm.openai_compat_call",
        provider=provider,
        model=model,
        msg_count=len(msgs),
        max_tokens=max_tokens,
    )

    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            f"{settings.KIMI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.KIMI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    logger.info(
        "llm.openai_compat_response",
        provider=provider,
        model=model,
        tokens_in=usage.get("prompt_tokens", 0),
        tokens_out=usage.get("completion_tokens", 0),
        response_length=len(text),
    )
    return text


async def _call_gemini(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.3,
    on_chunk=None,  # optional async callback(chars_so_far: int, latest_chunk: str)
    on_retry=None,  # optional async callback(attempt: int, wait_seconds: int, status: int)
) -> str:
    """Call Google Gemini via the streaming SSE endpoint (streamGenerateContent).

    Streams tokens as they arrive so callers can show live progress.
    Requires INTEGRATION_GEMINI_API_KEY in .env.
    """
    import httpx

    if not settings.GEMINI_API_KEY:
        raise RuntimeError("INTEGRATION_GEMINI_API_KEY is not set. Add it to integration/.env")

    model = model or settings.GEMINI_MODEL
    max_tokens = max_tokens or 8192

    # Build Gemini native contents format (user/model roles only — no system role)
    contents = []
    for msg in messages:
        role = "user" if msg.get("role") == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg.get("content", "")}]})

    gen_config: dict[str, Any] = {
        "maxOutputTokens": max_tokens,
        "temperature": temperature,
    }
    # Enable thinking for models that support it (gemini-3-flash, gemini-2.5-flash/pro).
    # thinkingBudget=-1 = dynamic (model decides how much to think per request).
    # thinkingBudget=0  = disabled (faster but less accurate — not recommended for code gen).
    if settings.GEMINI_THINKING_BUDGET != 0:
        gen_config["thinkingConfig"] = {"thinkingBudget": settings.GEMINI_THINKING_BUDGET}

    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": gen_config,
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    # Use streaming endpoint — alt=sse streams SSE events as tokens are generated

    logger.info(
        "llm.gemini_stream_call",
        model=model,
        msg_count=len(contents),
        max_tokens=max_tokens,
    )

    # Retry config — 503 (high demand) and 429 (rate limit) are both transient.
    # 429 (rate limit) needs longer waits than 503 (momentary overload).
    # After exhausting retries on the primary model, fall back to gemini-2.0-flash
    # which has a separate (higher) rate-limit quota before giving up entirely.
    _RETRYABLE_STATUSES = {429, 503}
    # Delays indexed by attempt number (0 = first try, no delay).
    # 429: wait longer — quota windows are typically 60s on free/low-tier keys.
    _RETRY_DELAYS_429 = [20, 60, 120]  # 3 retries: 20s, 60s, 120s
    _RETRY_DELAYS_503 = [5, 15, 30]  # 3 retries: 5s, 15s, 30s (transient overload)

    # Fallback model when primary exhausts 429 retries (higher quota tier)
    _FALLBACK_MODEL = "gemini-2.0-flash"

    import asyncio as _asyncio

    # We'll try the primary model first, then the fallback model on 429 exhaustion.
    _models_to_try = [model]
    if model != _FALLBACK_MODEL:
        _models_to_try.append(_FALLBACK_MODEL)

    _last_error: str | None = None

    for _model_idx, _current_model in enumerate(_models_to_try):
        # Build URL for this model
        _current_url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{_current_model}:streamGenerateContent?alt=sse&key={settings.GEMINI_API_KEY}"
        )
        if _model_idx > 0:
            logger.warning(
                "llm.gemini_model_fallback",
                from_model=_models_to_try[_model_idx - 1],
                to_model=_current_model,
                reason=_last_error,
            )

        _last_status: int | None = None
        _succeeded = False

        for _attempt, _delay in enumerate([0, *_RETRY_DELAYS_429], start=1):
            # Choose delay list based on last seen status
            _delays = _RETRY_DELAYS_429 if _last_status == 429 else _RETRY_DELAYS_503

            _actual_delay = (
                0 if _attempt == 1 else _delays[_attempt - 2] if _attempt - 2 < len(_delays) else _delays[-1]
            )

            if _actual_delay:
                _status_str = "429 rate-limit" if _last_status == 429 else f"{_last_status}"
                logger.warning(
                    "llm.gemini_retrying",
                    model=_current_model,
                    attempt=_attempt,
                    wait_seconds=_actual_delay,
                    status=_last_status,
                )
                if on_retry:
                    with contextlib.suppress(Exception):
                        await on_retry(_attempt, _actual_delay, _last_status or 429)
                await _asyncio.sleep(_actual_delay)

            collected_chunks: list[str] = []
            total_chars = 0

            try:
                async with httpx.AsyncClient(timeout=300.0) as client:
                    async with client.stream(
                        "POST",
                        _current_url,
                        headers={"Content-Type": "application/json"},
                        json=payload,
                    ) as resp:
                        if resp.status_code in _RETRYABLE_STATUSES:
                            body = await resp.aread()
                            err = body.decode("utf-8", errors="replace")[:300]
                            _last_status = resp.status_code
                            _last_error = f"{resp.status_code}: {err[:100]}"
                            logger.warning(
                                "llm.gemini_transient_error",
                                model=_current_model,
                                status=resp.status_code,
                                attempt=_attempt,
                                body=err,
                            )
                            # Max retries for this model exhausted — break inner loop
                            # to try fallback model (for 429) or give up (for 503)
                            _max_retries = len(_RETRY_DELAYS_429) if resp.status_code == 429 else len(_RETRY_DELAYS_503)
                            if _attempt > _max_retries:
                                break  # break inner retry loop → try next model or give up
                            continue  # go to next retry attempt

                        if resp.status_code != 200:
                            body = await resp.aread()
                            err = body.decode("utf-8", errors="replace")[:300]
                            logger.error(
                                "llm.gemini_error",
                                model=_current_model,
                                status=resp.status_code,
                                body=err,
                            )
                            raise RuntimeError(f"Gemini API error {resp.status_code}: {err}")

                        # Use aiter_lines() — gives one SSE line at a time
                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            data_str = line[len("data:") :].strip()
                            if not data_str or data_str == "[DONE]":
                                continue
                            try:
                                data = json.loads(data_str)
                                candidates = data.get("candidates") or []
                                if not candidates:
                                    continue
                                candidate = candidates[0]
                                content = candidate.get("content") or {}
                                parts = content.get("parts") or []
                                chunk_text = parts[0].get("text", "") if parts else ""
                                if chunk_text:
                                    collected_chunks.append(chunk_text)
                                    total_chars += len(chunk_text)
                                    if on_chunk:
                                        with contextlib.suppress(Exception):
                                            await on_chunk(total_chars, chunk_text)
                            except Exception as parse_exc:
                                logger.debug(
                                    "llm.gemini_sse_parse_error",
                                    line_preview=line[:120],
                                    error=str(parse_exc),
                                )

                # Successful response — mark success and break out of retry loop
                _succeeded = True
                break

            except RuntimeError:
                raise  # non-retryable errors bubble up immediately
            except Exception as conn_exc:
                # Network-level errors (timeout, connection reset) — treat as retryable
                _last_error = str(conn_exc)
                if _attempt > len(_RETRY_DELAYS_503):
                    raise RuntimeError(f"Gemini request failed after {_attempt} attempts: {conn_exc}") from conn_exc
                logger.warning(
                    "llm.gemini_connection_error_retrying",
                    model=_current_model,
                    attempt=_attempt,
                    error=str(conn_exc),
                )
                continue

        if _succeeded:
            break  # break outer model loop — we have a good response

    if not _succeeded:
        raise RuntimeError(
            f"Gemini API gave up after trying models {_models_to_try} — "
            f"last error: {_last_error}. "
            f"The primary model hit rate limits. Try again in a few minutes."
        )

    text = "".join(collected_chunks)
    logger.info(
        "llm.gemini_stream_response",
        model=model,
        response_length=len(text),
        chunks=len(collected_chunks),
    )
    return text


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences that some LLMs add around Python code."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        lines = lines[1:] if lines[0].startswith("```") else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return text


async def call_llm_tests(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.3,
    expect_code: bool = True,
    tenant_id: str | None = None,
) -> str:
    """LLM call specifically for test generation.

    Routes to the configured test LLM (INTEGRATION_TEST_LLM_MODE):
      gemini  → Google Gemini (fast, ~10-20s, cheap — recommended)
      kimi    → Moonshot AI Kimi (fast, ~20-40s, cheap)
      mcp     → shielva-mcp LiteLLM router (requires tenant_id=)
      <empty> → falls back to the default LLM (Claude CLI / API)
    """
    test_mode = settings.TEST_LLM_MODE.lower()

    # ── Gemini (thinking model) → Claude CLI fallback chain ─────────────
    if test_mode == "gemini":
        try:
            text = await _call_gemini(
                messages,
                system=system,
                model=model or settings.TEST_GEMINI_MODEL,  # use thinking model for tests
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return _strip_code_fences(text) if expect_code else text
        except RuntimeError as _gemini_err:
            _msg = str(_gemini_err)
            if "503" in _msg or "429" in _msg or "gave up" in _msg or "402" in _msg:
                logger.warning("llm.gemini_unavailable_fallback_to_cli", error=_msg[:200])
                # Fall through to Claude CLI below
            else:
                raise

    # Default: use whatever LLM mode is configured globally (Claude CLI / API / MCP)
    return await call_llm(
        messages,
        system=system,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        expect_code=expect_code,
        tenant_id=tenant_id,
    )


async def call_llm_fix(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.3,
    expect_code: bool = True,
    on_chunk=None,  # optional async callback(chars_so_far: int, chunk: str) for live progress
    tenant_id: str | None = None,
) -> str:
    """LLM call for AI fix operations (Attempt Fix on any step).

    When INTEGRATION_TEST_LLM_MODE=gemini, streams tokens via Gemini's SSE endpoint
    so callers receive live progress via on_chunk.
    When INTEGRATION_TEST_LLM_MODE=mcp, routes through shielva-mcp's LiteLLM router.
    Falls back to CLI otherwise.
    """
    test_mode = settings.TEST_LLM_MODE.lower()

    # ── Gemini → Claude CLI fallback chain ─────────────────────────────
    if test_mode == "gemini":
        try:
            text = await _call_gemini(
                messages,
                system=system,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                on_chunk=on_chunk,
            )
            return _strip_code_fences(text) if expect_code else text
        except RuntimeError as _gemini_err:
            _msg = str(_gemini_err)
            if "503" in _msg or "429" in _msg or "gave up" in _msg or "402" in _msg:
                logger.warning("llm.gemini_unavailable_fallback_to_cli", error=_msg[:200])
                # Fall through to Claude CLI below
            else:
                raise

    # In MCP mode: route fixes through the MCP fix-agent endpoint (uses tool-calling loop)
    # which gives smarter, tool-assisted fixes vs a plain LLM text completion call.
    if settings.LLM_MODE.lower() == "mcp":
        effective_tenant_id = tenant_id or get_llm_tenant_id()
        if effective_tenant_id:
            from integration.services.mcp_client import fix_code_via_mcp_agent

            # Extract broken code from messages (last user message content)
            broken_code = ""
            error_output = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    broken_code = content[:8000]
                    break
            # Error output is best-effort from system prompt context
            error_output = system[:3000]

            # Guard: if we couldn't extract any code, fall through to standard call_llm
            if not broken_code.strip():
                logger.warning("llm.fix_mcp_no_code_fallback", msg_count=len(messages))
                return await call_llm(
                    messages,
                    system=system,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    expect_code=expect_code,
                    tenant_id=tenant_id,
                )

            return await fix_code_via_mcp_agent(
                broken_code=broken_code,
                error_output=error_output,
                tenant_id=effective_tenant_id,
                model=model,
                max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
                temperature=temperature,
            )

    return await call_llm(
        messages,
        system=system,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        expect_code=expect_code,
        tenant_id=tenant_id,
    )


# ── API mode ─────────────────────────────────────────────────────────

_api_client = None


def _get_api_client():
    global _api_client
    if _api_client is None:
        from anthropic import AsyncAnthropic

        _api_client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _api_client


async def _call_api(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.3,
) -> str:
    """Call Claude via the Anthropic API (requires API key)."""
    client = _get_api_client()
    model = model or settings.LLM_MODEL
    max_tokens = max_tokens or settings.LLM_MAX_TOKENS

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system

    logger.info("llm.api_call", model=model, msg_count=len(messages), max_tokens=max_tokens)

    response = await client.messages.create(**kwargs)

    text = ""
    for block in response.content:
        if block.type == "text":
            text += block.text

    logger.info(
        "llm.api_response",
        model=model,
        tokens_in=response.usage.input_tokens,
        tokens_out=response.usage.output_tokens,
    )
    return text


# ── Worker mode (Redis queue → remote Claude CLI) ───────────────────


def _build_prompt(messages: list[dict[str, str]], system: str = "") -> str:
    """Combine system + messages into a single prompt string."""
    parts = []
    if system:
        parts.append(f"<system>\n{system}\n</system>\n")
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            parts.append(content)
        elif role == "assistant":
            parts.append(f"[Previous assistant response]: {content}")
    return "\n\n".join(parts)


async def _call_worker(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    max_tokens: int | None = None,
) -> str:
    """Push LLM job to Redis queue and wait for the worker to process it.

    The worker (llm_worker.py) runs on a machine with Claude CLI logged in.
    """
    from integration.services.llm_queue import enqueue_llm_job, wait_for_result

    prompt = _build_prompt(messages, system)
    job_id = await enqueue_llm_job(prompt, system="", max_tokens=max_tokens)

    logger.info("llm.worker_enqueued", job_id=job_id, prompt_length=len(prompt))

    text = await wait_for_result(
        job_id,
        timeout=settings.LLM_WORKER_TIMEOUT,
    )

    logger.info("llm.worker_response", job_id=job_id, response_length=len(text))
    return text


# ── Public API (mode-agnostic) ───────────────────────────────────────

_CODE_STARTS = ("import ", "from ", "#", '"""', "class ", "def ", "async ")


def _looks_like_code(text: str) -> bool:
    """Return True if text looks like it starts with Python code (not reasoning)."""
    if not text or len(text) < 50:
        return False
    stripped = text.lstrip()
    return stripped.startswith(_CODE_STARTS)


async def call_llm(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.3,
    expect_code: bool = True,
    tenant_id: str | None = None,
) -> str:
    """Call the configured LLM and return the text response.

    Automatically uses the mode set in INTEGRATION_LLM_MODE:
      - cli:    Local Claude CLI (dev machine with Max plan)
      - worker: Redis queue → remote worker with Claude CLI (production, $0)
      - api:    Anthropic API (production, requires ANTHROPIC_API_KEY)
      - mcp:    shielva-mcp LiteLLM router (uses MCP's key + fallback chain)
                Requires tenant_id= to be passed for X-Tenant-ID header.

    If expect_code=True (default) and the response doesn't look like code,
    retries once with a stricter prompt.
    """
    mode = settings.LLM_MODE.lower()

    async def _do_call(msgs, sys_prompt):
        if mode == "cli":
            return await _call_cli(msgs, system=sys_prompt, max_tokens=max_tokens)
        if mode == "worker":
            return await _call_worker(msgs, system=sys_prompt, max_tokens=max_tokens)
        if mode == "api":
            return await _call_api(
                msgs,
                system=sys_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        if mode == "mcp":
            # Resolve tenant_id: explicit param wins, else read from ContextVar
            # set once per execution by codegen_service.set_llm_tenant_id().
            effective_tenant_id = tenant_id or get_llm_tenant_id()
            if not effective_tenant_id:
                ctx_val = repr(get_llm_tenant_id())
                raise ValueError(
                    f"LLM_MODE=mcp requires a tenant_id but got empty. "
                    f"ContextVar value={ctx_val}, explicit param={tenant_id!r}. "
                    f"Ensure set_llm_tenant_id(session.tenant_id) is called "
                    f"at the start of execute_plan() or attempt_fix_step()."
                )
            from integration.services.mcp_client import call_llm_via_mcp

            return await call_llm_via_mcp(
                msgs,
                system=sys_prompt,
                tenant_id=effective_tenant_id,
                model=model,
                max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
                temperature=temperature,
            )
        raise ValueError(f"Unknown LLM_MODE: '{mode}'. Use 'cli', 'worker', 'api', or 'mcp'.")

    text = await _do_call(messages, system)

    # If expecting code and got non-code response, retry once with a stricter prompt
    if expect_code and not _looks_like_code(text):
        logger.warning(
            "llm.non_code_response",
            response_preview=text[:100] if text else "(empty)",
            retrying=True,
        )

        retry_system = system + (
            "\n\n## CRITICAL REMINDER\n"
            "You MUST respond with ONLY Python code. No explanations, no markdown, no XML.\n"
            "The VERY FIRST character of your response must be part of a Python import, comment, or class definition.\n"
            "Do NOT include any text before or after the code."
        )

        text = await _do_call(messages, retry_system)

        if not _looks_like_code(text):
            logger.error(
                "llm.retry_also_non_code",
                response_preview=text[:100] if text else "(empty)",
            )

    return text


async def call_llm_streaming(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    max_tokens: int | None = None,
    on_chunk=None,
    expect_code: bool = True,
    tenant_id: str | None = None,
) -> str:
    """Call the LLM with streaming progress via on_chunk callback.

    on_chunk(text_so_far, chunk) is called as output arrives.
    Falls back to non-streaming for non-CLI/non-Gemini modes.
    Returns the final full text.

    Set expect_code=False for multi-file restructure responses that use
    ===FILE: delimiters rather than plain Python code.
    """
    mode = settings.LLM_MODE.lower()

    if mode == "cli" and on_chunk:
        return await _call_cli_streaming(
            messages,
            system=system,
            max_tokens=max_tokens,
            on_chunk=on_chunk,
        )

    # For worker/api/mcp modes, fall back to non-streaming call
    return await call_llm(
        messages,
        system=system,
        max_tokens=max_tokens,
        expect_code=expect_code,
        tenant_id=tenant_id,
    )


def _strip_json_fences(raw: str) -> str:
    """Strip markdown code fences from a string and return clean text."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


async def call_llm_json(
    messages: list[dict[str, str]],
    *,
    system: str = "",
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float = 0.2,
    tenant_id: str | None = None,
) -> Any:
    """Call the primary LLM and parse the response as JSON.

    If the primary LLM (Claude CLI) returns empty — which happens when the Claude
    Max account is rate-limited from too many concurrent/rapid calls — falls back
    to Gemini automatically so plan generation never hard-fails.

    Strips markdown code fences if present, then parses.
    """
    raw = await call_llm(
        messages,
        system=system,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        expect_code=False,  # We want JSON, not Python code — skip the code-look check
        tenant_id=tenant_id,
    )

    # Fallback: if Claude CLI returned empty (rate-limited / silent failure),
    # retry once via Gemini which is an HTTP API and not subject to CLI concurrency limits.
    if not raw and settings.GEMINI_API_KEY:
        logger.warning("llm.json_claude_empty_fallback_gemini")
        raw = await _call_gemini(
            messages,
            system=system,
            max_tokens=max_tokens or 8192,
            temperature=temperature,
        )

    if not raw:
        raise ValueError(
            "LLM returned an empty response. "
            "If using CLI mode, ensure the Claude CLI is authenticated (`claude login`) "
            "and that INTEGRATION_LLM_MODE is correct. "
            "Set INTEGRATION_GEMINI_API_KEY as a fallback for plan generation."
        )

    text = _strip_json_fences(raw)

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("llm.json_parse_failed", error=str(exc), raw_preview=raw[:300])
        raise ValueError(f"LLM response is not valid JSON: {exc}\n\nRaw response preview: {raw[:300]}") from exc
