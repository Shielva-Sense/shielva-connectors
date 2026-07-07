"""platform_default.claude.skill — Shielva's canonical Claude LLM skill.

Wraps the integration service's llm_client with:
  - Conversation memory  — passes full message history to Claude each turn so it
                           recalls prior context (initial plan, previous replans, etc.)
  - "Call only when needed" guard — checks R2 progress.json before calling Claude.
                           If a plan is already cached (plan_generated=true), callers
                           can skip the LLM call entirely and serve from R2.
  - Consistent structured logging via structlog.

Usage pattern (planning service):

    from platform_default.claude.skill import ClaudeSkill
    from integration.services import r2_service

    skill = ClaudeSkill.from_integration_settings()

    # Guard: only call Claude if no cached plan exists
    if await skill.plan_is_cached(provider, service, tenant_id, r2_service):
        return await r2_service.get_history(provider, service, tenant_id)

    # Generate with conversation memory
    prior_history = session.get("conversation_history", [])
    llm_result, updated_history = await skill.chat(
        user_message="Build a connector plan for Slack.",
        system=system_prompt,
        prior_history=prior_history,
        parse_json=True,
    )
    # Persist updated_history to session so replan can recall it
    await sessions_collection().update_one(
        {"_id": oid}, {"$set": {"conversation_history": updated_history}}
    )
"""

import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class ClaudeSkill:
    """Stateless Claude skill with conversation memory management.

    Each `chat()` call accepts prior_history (list of {role, content} dicts),
    appends the new user + assistant turn, and returns the full updated history.
    The caller is responsible for persisting history between calls (e.g. in MongoDB).

    In CLI mode (the default for local dev):
      The history is serialised into the prompt text so Claude sees prior context.
    In API mode:
      The history is passed as the messages array — true multi-turn conversation.
    In worker mode:
      Same serialisation as CLI; the remote worker receives the flattened prompt.
    """

    def __init__(
        self,
        *,
        mode: str = "cli",
        model: str = "claude-sonnet-4-20250514",
        api_key: str = "",
        cli_path: str = "claude",
        max_tokens: int = 8192,
    ):
        self.mode = mode
        self.model = model
        self.api_key = api_key
        self.cli_path = cli_path
        self.max_tokens = max_tokens

    # ── Factory ────────────────────────────────────────────────────────

    @classmethod
    def from_integration_settings(cls) -> "ClaudeSkill":
        """Load config from integration service environment settings."""
        from integration.core.config import settings

        return cls(
            mode=settings.LLM_MODE,
            model=settings.LLM_MODEL,
            api_key=settings.ANTHROPIC_API_KEY,
            cli_path=settings.CLAUDE_CLI_PATH,
            max_tokens=settings.LLM_MAX_TOKENS,
        )

    # ── Core chat method ──────────────────────────────────────────────

    async def chat(
        self,
        user_message: str,
        *,
        system: str = "",
        prior_history: list[dict[str, str]] | None = None,
        parse_json: bool = False,
    ) -> tuple[Any, list[dict[str, str]]]:
        """Call Claude with full conversation history.

        Args:
            user_message:  The new user turn for this call.
            system:        System prompt (injected before all messages).
            prior_history: Previous {role, content} turns. Claude will recall
                           everything it said before, so replans get full context
                           of the initial plan + any prior feedback.
            parse_json:    Strip markdown fences and parse response as JSON.

        Returns:
            (response, updated_history)
            - response:         str | dict | list  (dict/list when parse_json=True)
            - updated_history:  prior_history extended with this turn's user +
                                assistant messages — persist this to MongoDB.
        """
        history = list(prior_history or [])
        messages = [*history, {"role": "user", "content": user_message}]

        logger.info(
            "claude_skill.chat",
            mode=self.mode,
            prior_turns=len(history) // 2,
            user_msg_len=len(user_message),
            parse_json=parse_json,
        )

        from integration.services.llm_client import call_llm, call_llm_json

        if parse_json:
            response = await call_llm_json(
                messages,
                system=system,
                model=self.model,
                max_tokens=self.max_tokens,
            )
            response_text = json.dumps(response, default=str)
        else:
            response_text = await call_llm(
                messages,
                system=system,
                model=self.model,
                max_tokens=self.max_tokens,
            )
            response = response_text

        # Append this turn so the next call gets full history
        updated_history = [*messages, {"role": "assistant", "content": response_text}]

        logger.info(
            "claude_skill.chat_done",
            response_len=len(response_text),
            total_turns=len(updated_history) // 2,
        )

        return response, updated_history

    # ── R2 cache guard ────────────────────────────────────────────────

    async def plan_is_cached(
        self,
        provider: str,
        service: str,
        tenant_id: str,
        r2_svc: Any,
    ) -> bool:
        """Check R2 progress.json — returns True if plan_generated=true.

        When True the caller should skip Claude and serve the cached plan from R2.
        This is the primary guard that prevents unnecessary LLM calls.
        """
        history = await r2_svc.get_history(provider, service, tenant_id)
        is_cached = history is not None and history.get("plan_generated", False)

        logger.info(
            "claude_skill.cache_check",
            provider=provider,
            service=service,
            tenant_id=tenant_id,
            is_cached=is_cached,
        )
        return is_cached
