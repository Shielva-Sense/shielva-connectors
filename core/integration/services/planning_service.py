"""Integration Builder — AI Planning Service.

Generates structured integration plans via Claude, then stores them on the session.
"""

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncGenerator
from datetime import UTC
from typing import Any

import structlog
from bson import ObjectId

from integration.core.config import settings
from integration.data.catalog import get_service_detail
from integration.db.database import sessions_collection
from integration.prompts.planning_prompt import (
    BASE_CONNECTOR_INTERFACE,
    PLANNING_SYSTEM_PROMPT,
    REPLAN_SYSTEM_PROMPT,
)
from integration.schemas.models import (
    PlanDocument,
    PlanStep,
    SessionStatus,
    StepComment,
    StepStatus,
    StepType,
)
from integration.services import r2_service
from integration.services.codegen_service import (
    _service_slug,
    _slug_from_connector_name,
)
from integration.services.guidelines_service import get_active_guidelines
from integration.services.llm_client import set_llm_tenant_id
from platform_default.claude.skill import ClaudeSkill

# ── Plan offload (Phase 4) ────────────────────────────────────────────
# Mongo keeps only `{version, steps:[{index,title,type,status}]}` — the
# minimal subset the Manage Connectors list + Builder status badges read on
# every render. The FULL plan body (per-step description, config dict,
# install_fields, methods, etc.) is offloaded to R2 at
# CONNECTORS/{provider}/{slug}/{session_id}/plan_full.json.
#
# Read paths in session_routes.get_session() merge R2 fields back over the
# slim Mongo steps when the Builder opens a session, so the UI sees the same
# shape as before; codegen_service hydrates plan from R2 on first read.

_PLAN_STEP_SLIM_KEYS = ("index", "title", "type", "status")


def _slim_plan(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a tiny copy of a plan dict suitable for Mongo storage.

    Keeps top-level metadata (version) and only the per-step fields the list
    view + Builder badge ever read. Everything else (description, config,
    install_fields, methods, scopes) is dropped — it must be fetched from R2
    `plan_full.json` by the readers that need it.
    """
    if not plan or not isinstance(plan, dict):
        return plan
    steps_in = plan.get("steps") or []
    slim_steps = []
    for s in steps_in:
        if not isinstance(s, dict):
            slim_steps.append(s)
            continue
        slim_steps.append({k: s[k] for k in _PLAN_STEP_SLIM_KEYS if k in s})
    slim = {k: v for k, v in plan.items() if k not in ("steps",)}
    slim["steps"] = slim_steps
    slim["_r2_offloaded"] = True
    return slim


async def persist_conversation_history(
    *,
    session_id: str,
    provider: str,
    service_slug: str,
    history: list[dict[str, Any]],
) -> Any:
    """Write the full conversation history to R2 and return a slim Mongo pointer.

    Mongo keeps only ``{r2_offloaded:True, turn_count:N, last_turn_at:isoformat}``;
    R2 holds the array. The Builder read path (session_routes.get_session) and
    any reader needing the full history hydrate from R2 on demand.

    On R2 failure we fall through to storing the full list in Mongo so we
    never lose conversational context — degraded mode, not data loss.
    """
    from datetime import datetime as _dt

    if history is None:
        return []
    try:
        await r2_service.save_conversation_history(
            provider=provider,
            service_slug=service_slug,
            session_id=session_id,
            history=history,
        )
        return {
            "_r2_offloaded": True,
            "turn_count": len(history),
            "last_turn_at": _dt.utcnow().isoformat(),
        }
    except Exception as exc:
        logger.warning(
            "conversation_history.r2_save_failed",
            session_id=session_id,
            error=str(exc),
        )
        return history


async def hydrate_conversation_history(
    *,
    session: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the full history list from a session dict.

    If Mongo holds the offload pointer, pull the array from R2; otherwise
    return whatever's embedded in Mongo (legacy sessions). Always returns
    a list — never None.
    """
    ch = session.get("conversation_history")
    if isinstance(ch, list):
        return ch
    if not isinstance(ch, dict) or not ch.get("_r2_offloaded"):
        return []
    provider = session.get("provider", "")
    service_slug = session.get("service_slug") or session.get("service") or ""
    sid_val = session.get("_id")
    sid = str(sid_val) if sid_val is not None else ""
    if not provider or not service_slug or not sid:
        return []
    try:
        arr = await r2_service.get_conversation_history(
            provider=provider,
            service_slug=service_slug,
            session_id=sid,
        )
        if isinstance(arr, list):
            return arr
    except Exception as exc:
        logger.warning("conversation_history.r2_hydrate_failed", session_id=sid, error=str(exc))
    return []


async def persist_plan(
    *,
    session_id: str,
    provider: str,
    service_slug: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Write the FULL plan to R2 and return the slim version for Mongo.

    Callers should `$set: {"plan": <returned slim>}` on the session. The
    Builder + codegen read paths lazily hydrate the full step config back
    in. R2 write failures fall through to the full dict so we never lose
    plan data even if storage is briefly unreachable.
    """
    if not plan:
        return plan
    try:
        await r2_service.save_plan_full(
            provider=provider,
            service_slug=service_slug,
            session_id=session_id,
            plan=plan,
        )
        return _slim_plan(plan)
    except Exception as exc:
        logger.warning("plan.r2_save_failed", session_id=session_id, error=str(exc))
        return plan


# Shared skill instance — loaded once at module import time from env settings
_claude = ClaudeSkill.from_integration_settings()

logger = structlog.get_logger(__name__)


# ── Operation verb extractor ──────────────────────────────────────────

# Verbs that map directly to connector method names
_OPERATION_VERBS = [
    "list",
    "send",
    "delete",
    "create",
    "get",
    "fetch",
    "update",
    "read",
    "search",
    "find",
    "download",
    "upload",
    "move",
    "copy",
    "archive",
    "restore",
    "mark",
    "label",
    "forward",
    "reply",
    "draft",
    "compose",
    "attach",
    "filter",
    "watch",
    "unsubscribe",
    "subscribe",
    "publish",
    "post",
    "put",
    "patch",
    "remove",
    "add",
    "insert",
    "upsert",
]


def _extract_user_operations(raw_prompt: str, service_slug: str) -> list[str]:
    """Extract user-requested operations from an informal prompt and return method names.

    E.g. "need a gmail connector to list, send, delete email" →
         ["list_emails", "send_email", "delete_email"]
    """
    if not raw_prompt:
        return []

    prompt_lower = raw_prompt.lower()
    words = re.findall(r"\b\w+\b", prompt_lower)

    # Determine the object noun from the service slug
    # e.g. "gmail" → "email", "slack" → "message", "drive" → "file"
    _SERVICE_NOUNS: dict = {
        "gmail": "email",
        "mail": "email",
        "email": "email",
        "slack": "message",
        "teams": "message",
        "chat": "message",
        "drive": "file",
        "storage": "file",
        "s3": "object",
        "calendar": "event",
        "sheets": "sheet",
        "docs": "document",
        "contacts": "contact",
        "crm": "contact",
        "github": "issue",
        "jira": "ticket",
        "linear": "issue",
        "stripe": "payment",
        "payments": "payment",
        "notion": "page",
        "confluence": "page",
        "hubspot": "contact",
        "salesforce": "record",
    }
    # Try to find noun from slug parts
    slug_parts = re.split(r"[_\-]", service_slug.replace("_connector", ""))
    base_noun = "item"
    for part in slug_parts:
        if part in _SERVICE_NOUNS:
            base_noun = _SERVICE_NOUNS[part]
            break

    # Also check if prompt contains an explicit noun near a verb
    _COMMON_NOUNS = [
        "email",
        "emails",
        "message",
        "messages",
        "file",
        "files",
        "document",
        "documents",
        "event",
        "events",
        "contact",
        "contacts",
        "record",
        "records",
        "item",
        "items",
        "object",
        "objects",
        "ticket",
        "tickets",
        "issue",
        "issues",
        "payment",
        "payments",
        "page",
        "pages",
        "post",
        "posts",
        "draft",
        "drafts",
    ]

    found_methods: list[str] = []
    for i, word in enumerate(words):
        if word in _OPERATION_VERBS:
            # Look ahead 1–2 words for a noun
            noun = base_noun
            for j in range(i + 1, min(i + 3, len(words))):
                candidate = words[j].rstrip("s")  # singularize
                candidate_plural = words[j]
                if candidate_plural in _COMMON_NOUNS or candidate in _COMMON_NOUNS:
                    noun = candidate if not candidate.endswith("s") else candidate_plural.rstrip("s")
                    break
            method = f"{word}_{noun}"
            if method not in found_methods:
                found_methods.append(method)

    return found_methods


# ── Guidelines helper ─────────────────────────────────────────────────


async def _get_guidelines_for_planning() -> tuple:
    """Cache guidelines to a tmp file and return a file-read directive instead of inline content.

    Instead of injecting 28k of guidelines into the prompt, we write them to
    /tmp/shielva_guidelines_{version}.md (keyed by version so it's only written once)
    and return a short instruction for Claude to read the file itself.

    Returns:
        Tuple of (guidelines_directive: str, version: str).
    """
    import os as _os
    import tempfile

    try:
        record = await get_active_guidelines()
        content = record.get("content", "")
        version = record.get("version", "unknown")
        if not content:
            return "", version

        # Write to tmp once per version — idempotent
        safe_version = version.replace("/", "_").replace(" ", "_")
        tmp_path = _os.path.join(tempfile.gettempdir(), f"shielva_guidelines_{safe_version}.md")
        if not _os.path.exists(tmp_path):
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(content)

        directive = (
            f"CODING GUIDELINES are cached at: `{tmp_path}`\n"
            f"Read that file before planning so your step descriptions follow the guidelines."
        )
        return directive, version
    except Exception as exc:
        logger.warning("plan.guidelines_fetch_failed", error=str(exc))
        return "", "unknown"


# ── Safe prompt formatter ────────────────────────────────────────────
def _safe_format(template: str, **kwargs: str) -> str:
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", value)
    return result


# ── Prompt reconstruction ─────────────────────────────────────────────

_RECONSTRUCT_SYSTEM = """\
You are a Shielva connector architect. Your job is to take a user's informal connector requirements
and expand them into a precise, structured requirements specification that a code generator can use.

Rules:
1. Extract every operation the user mentioned — list, send, delete, create, update, search, etc.
2. Name each operation as an explicit async method (snake_case, e.g. list_emails, send_email)
3. For each method specify: what it does, inputs, return type (NormalizedDocument list or void)
4. Enforce SOC: connector.py orchestrates only — no direct SDK calls, no data transforms inline
5. Enforce OCP: use lookup dicts not if/elif chains, use class constants for config/retries
6. Include: auth flow required (OAuth2/API key/basic), pagination strategy, error handling needs
7. Output ONLY the structured specification — no preamble, no markdown headings, plain paragraphs

Keep it concise (under 400 words). Do NOT write code.\
"""


async def reconstruct_user_prompt(
    raw_prompt: str,
    provider: str,
    service: str,
    auth_type: str,
) -> str:
    """Use Claude to expand a user's informal requirements into a structured spec.

    Takes a short free-text input like "list emails, send emails, delete emails"
    and returns a detailed requirements spec with explicit method names, SOC/OCP
    constraints, and data model expectations.

    Falls back to the original prompt if the LLM call fails.
    """
    if not raw_prompt or not raw_prompt.strip():
        return raw_prompt

    user_msg = (
        f"Connector: {provider} / {service} (auth: {auth_type})\n\n"
        f"User requirements (raw):\n{raw_prompt.strip()}\n\n"
        "Expand this into a structured connector requirements specification."
    )

    try:
        # Hard timeout: this is an external LLM call. Without it, a slow/unreachable
        # model hangs the /plan-prompt endpoint indefinitely (the bare try/except only
        # catches errors, not hangs). On timeout we honor the documented contract below
        # and fall back to the raw prompt rather than blocking plan generation.
        response, _ = await asyncio.wait_for(
            _claude.chat(user_msg, system=_RECONSTRUCT_SYSTEM),
            timeout=25.0,
        )
        reconstructed = (response or "").strip()
        if len(reconstructed) > 50:
            logger.info(
                "plan.prompt_reconstructed",
                provider=provider,
                service=service,
                original_len=len(raw_prompt),
                reconstructed_len=len(reconstructed),
            )
            return reconstructed
    except TimeoutError:
        logger.warning("plan.prompt_reconstruct_timeout", provider=provider, service=service)
    except Exception as exc:
        logger.warning("plan.prompt_reconstruct_failed", error=str(exc))

    return raw_prompt


# ── SSE helper ───────────────────────────────────────────────────────


def _sse(event_type: str, data: dict[str, Any]) -> str:
    """Format a Server-Sent Event string."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


# ── Step type validation ──────────────────────────────────────────────

VALID_STEP_TYPES = {e.value for e in StepType}


def _ensure_user_methods_in_write_connector(steps: list[PlanStep], user_prompt: str, service_slug: str) -> None:
    """Post-processing guard: ensure user-requested operations are in write_connector.config.methods.

    If the planning LLM missed injecting user-requested methods (list_emails, send_email, etc.),
    this function adds them directly to the write_connector step config so downstream prompts
    (generate_implementation_plan, write_connector) always receive the full method list.
    """
    if not user_prompt:
        return

    write_step = next((s for s in steps if s.type == StepType.WRITE_CONNECTOR), None)
    if not write_step:
        return

    if write_step.config is None:
        write_step.config = {}

    existing_methods: list[str] = write_step.config.get("methods", [])
    extracted = _extract_user_operations(user_prompt, service_slug)

    # Base abstract methods that always exist — don't count as user-requested
    _BASE_METHODS = {
        "install",
        "authorize",
        "health_check",
        "sync",
        "disconnect",
        "get_metadata",
    }

    added = []
    for method in extracted:
        # Skip if already present or is a base method
        if method not in existing_methods and method not in _BASE_METHODS:
            existing_methods.append(method)
            added.append(method)

    if added:
        write_step.config["methods"] = existing_methods
        logger.info(
            "plan.user_methods_injected",
            service_slug=service_slug,
            injected=added,
            total_methods=existing_methods,
        )


def _ensure_terminal_steps(steps: list[PlanStep]) -> None:
    """Enforce the canonical 7-step connector build workflow.

    Guaranteed order:
      1. generate_implementation_plan
      2. install_deps
      3. write_connector
      4. smoke_test
      5. generate_test_guidelines
      6. write_tests
      7. generate_metadata
    (followed by setup_instructions and version_upgrade which are appended separately)

    Also strips any run_tests steps — write_tests covers test execution internally.
    Modifies the list in-place.
    """
    # Strip run_tests — write_tests covers test execution; run_tests is redundant
    steps[:] = [s for s in steps if s.type != StepType.RUN_TESTS]

    has_write_connector = any(s.type == StepType.WRITE_CONNECTOR for s in steps)

    # 0a. Ensure generate_implementation_plan exists before write_connector
    has_impl_plan = any(s.type == StepType.GENERATE_IMPLEMENTATION_PLAN for s in steps)
    if has_write_connector and not has_impl_plan:
        write_connector_idx = next(i for i, s in enumerate(steps) if s.type == StepType.WRITE_CONNECTOR)
        steps.insert(
            write_connector_idx,
            PlanStep(
                index=write_connector_idx,
                type=StepType.GENERATE_IMPLEMENTATION_PLAN,
                title="Generate Implementation Plan",
                description=(
                    "Analyse the API spec, auth type, and user requirements to produce a detailed "
                    "implementation_plan.md — full method surface (SOC/OCP), exact package names, "
                    "per-method implementation guidelines, and error-handling strategy. "
                    "write_connector uses this document as its specification."
                ),
                estimated_duration_s=60,
                config={},
                status=StepStatus.PENDING,
            ),
        )

    # 0b. Ensure install_deps exists and comes AFTER generate_implementation_plan.
    # impl_plan Section 7 lists exact pip package names — install_deps reads from it.
    has_install_deps = any(s.type == StepType.INSTALL_DEPS for s in steps)
    if has_write_connector and not has_install_deps:
        impl_idx = next(i for i, s in enumerate(steps) if s.type == StepType.GENERATE_IMPLEMENTATION_PLAN)
        steps.insert(
            impl_idx + 1,
            PlanStep(
                index=impl_idx + 1,
                type=StepType.INSTALL_DEPS,
                title="Install Python Dependencies",
                description="Install required packages identified in the implementation plan. Package names are read from Section 7 of implementation_plan.md for accuracy.",
                estimated_duration_s=30,
                config={"packages": []},
                status=StepStatus.PENDING,
            ),
        )
    elif has_install_deps:
        # Move install_deps to after generate_implementation_plan if it comes before it
        impl_plan_idx = next(i for i, s in enumerate(steps) if s.type == StepType.GENERATE_IMPLEMENTATION_PLAN)
        install_deps_idx = next(i for i, s in enumerate(steps) if s.type == StepType.INSTALL_DEPS)
        if install_deps_idx < impl_plan_idx:
            step_obj = steps.pop(install_deps_idx)
            new_impl_idx = next(i for i, s in enumerate(steps) if s.type == StepType.GENERATE_IMPLEMENTATION_PLAN)
            steps.insert(new_impl_idx + 1, step_obj)

    # 0b-ii. Ensure compliance_check exists immediately after write_connector (before smoke_test)
    has_compliance_check = any(s.type == StepType.COMPLIANCE_CHECK for s in steps)
    if has_write_connector and not has_compliance_check:
        write_connector_idx = next(i for i, s in enumerate(steps) if s.type == StepType.WRITE_CONNECTOR)
        steps.insert(
            write_connector_idx + 1,
            PlanStep(
                index=write_connector_idx + 1,
                type=StepType.COMPLIANCE_CHECK,
                title="SOC/OCP Compliance Audit",
                description=(
                    "Audit connector.py against all SOC (Separation of Concerns) and OCP (Open/Closed Principle) rules. "
                    "Calculate compliance percentage from 10-point checklist (5 SRP + 5 OCP). "
                    "Compliance must be >95% (10/10 — zero violations) to pass. "
                    "Automatically fix any violations found and re-audit until the gate passes."
                ),
                estimated_duration_s=60,
                config={"required_score": 10, "max_score": 10, "threshold_pct": 95},
                status=StepStatus.PENDING,
            ),
        )

    # 0c. Ensure smoke_test exists immediately after compliance_check (or write_connector)
    has_smoke_test = any(s.type == StepType.SMOKE_TEST for s in steps)
    if has_write_connector and not has_smoke_test:
        # Insert after compliance_check if it exists, otherwise after write_connector
        ref_type = StepType.COMPLIANCE_CHECK if True else StepType.WRITE_CONNECTOR
        ref_idx = next(
            (i for i, s in enumerate(steps) if s.type == ref_type),
            next(i for i, s in enumerate(steps) if s.type == StepType.WRITE_CONNECTOR),
        )
        steps.insert(
            ref_idx + 1,
            PlanStep(
                index=write_connector_idx + 1,
                type=StepType.SMOKE_TEST,
                title="Smoke Test Connector",
                description=(
                    "Import the generated connector in a subprocess with all network calls mocked. "
                    "Verifies the connector class imports correctly, instantiates, and install() "
                    "returns a valid ConnectorStatus without making real network calls."
                ),
                estimated_duration_s=20,
                config={},
                status=StepStatus.PENDING,
            ),
        )

    # 0d. Ensure generate_test_guidelines exists before write_tests
    has_write_tests = any(s.type == StepType.WRITE_TESTS for s in steps)
    has_gen_guidelines = any(s.type == StepType.GENERATE_TEST_GUIDELINES for s in steps)
    if has_write_tests and not has_gen_guidelines:
        write_tests_idx = next(i for i, s in enumerate(steps) if s.type == StepType.WRITE_TESTS)
        steps.insert(
            write_tests_idx,
            PlanStep(
                index=write_tests_idx,
                type=StepType.GENERATE_TEST_GUIDELINES,
                title="Generate Test Guidelines",
                description="Analyze the generated connector code and produce a connector-specific test guideline document used by the test writer.",
                estimated_duration_s=45,
                config={},
                status=StepStatus.PENDING,
            ),
        )

    # 0e. Ensure generate_metadata exists (before setup_instructions / at end of core steps)
    has_gen_metadata = any(s.type == StepType.GENERATE_METADATA for s in steps)
    if has_write_connector and not has_gen_metadata:
        # Insert after write_tests if it exists, otherwise at end
        ref_idx = next(
            (i for i, s in enumerate(steps) if s.type == StepType.WRITE_TESTS),
            len(steps) - 1,
        )
        steps.insert(
            ref_idx + 1,
            PlanStep(
                index=ref_idx + 1,
                type=StepType.GENERATE_METADATA,
                title="Generate Connector Metadata",
                description="Read the built connector.py and generate metadata/connector.json — install form schema, API catalogue, and Painter config.",
                estimated_duration_s=45,
                config={"version": "1.0.0"},
                status=StepStatus.PENDING,
            ),
        )

    # 1. Ensure setup_instructions exists (before version_upgrade)
    if not any(s.type == StepType.SETUP_INSTRUCTIONS for s in steps):
        ref_idx = next(
            (i for i, s in enumerate(steps) if s.type == StepType.VERSION_UPGRADE),
            len(steps),
        )
        steps.insert(
            ref_idx,
            PlanStep(
                index=ref_idx,
                type=StepType.SETUP_INSTRUCTIONS,
                title="Generate Setup Instructions",
                description="Research and generate connector-specific configuration guide — shows users exactly where to find credentials in the provider portal.",
                estimated_duration_s=45,
                config={},
                status=StepStatus.PENDING,
            ),
        )

    # 2. (deploy_form removed — credential testing handled by integration tests step)

    # 3. Ensure version_upgrade is last
    if not any(s.type == StepType.VERSION_UPGRADE for s in steps):
        steps.append(
            PlanStep(
                index=len(steps),
                type=StepType.VERSION_UPGRADE,
                title="Version Upgrade",
                description="Review changes and set the release version for this connector. Select patch, minor, or major version bump.",
                estimated_duration_s=30,
                config={"auto_suggest": True},
                status=StepStatus.PENDING,
            )
        )

    # Re-index
    for idx, s in enumerate(steps):
        s.index = idx


def _parse_steps(raw_steps: list[dict[str, Any]]) -> list[PlanStep]:
    """Parse and validate LLM-generated step list."""
    steps = []
    for i, raw in enumerate(raw_steps):
        step_type = raw.get("type", "")
        if step_type not in VALID_STEP_TYPES:
            logger.warning("plan.invalid_step_type", type=step_type, index=i)
            continue
        steps.append(
            PlanStep(
                index=i,
                type=StepType(step_type),
                title=raw.get("title", f"Step {i + 1}"),
                description=raw.get("description", ""),
                estimated_duration_s=raw.get("estimated_duration_s", 30),
                config=raw.get("config", {}),
                status=StepStatus.PENDING,
            )
        )
    # Re-index in case some were skipped
    for idx, step in enumerate(steps):
        step.index = idx
    return steps


def _extract_plan_parts(llm_result: Any) -> dict[str, Any]:
    """Extract steps, package_structure, recommended_features, default_config_fields from LLM output.

    Handles both old format (bare array) and new format (object with keys).
    """
    if isinstance(llm_result, list):
        # Old format: just an array of steps
        return {
            "raw_steps": llm_result,
            "package_structure": None,
            "recommended_features": [],
            "default_config_fields": [],
        }
    if isinstance(llm_result, dict):
        # New format: object with steps, package_structure, recommended_features, default_config_fields
        raw_steps = llm_result.get("steps", [])
        if not isinstance(raw_steps, list):
            raise ValueError(f"Expected 'steps' to be a list, got {type(raw_steps).__name__}")
        return {
            "raw_steps": raw_steps,
            "package_structure": llm_result.get("package_structure"),
            "recommended_features": llm_result.get("recommended_features", []),
            "default_config_fields": llm_result.get("default_config_fields", []),
        }
    raise ValueError(f"Expected JSON object or array from LLM, got {type(llm_result).__name__}")


def _fill_missing_plan_extras(
    parts: dict[str, Any],
    parsed_steps: "list[PlanStep]",
    auth_type: str = "",
) -> None:
    """Backfill recommended_features and default_config_fields from step configs when LLM omitted them.

    Mutates `parts` in-place. Called after _parse_steps so we have typed PlanStep objects.
    """
    if not parts.get("recommended_features"):
        for s in parsed_steps:
            feat_list = (s.config or {}).get("features") if s.config else None
            if feat_list and isinstance(feat_list, list):
                parts["recommended_features"] = [
                    {
                        "id": fid,
                        "label": fid.replace("_", " ").title(),
                        "recommended": True,
                        "category": "connector",
                        "description": "",
                    }
                    for fid in feat_list
                    if isinstance(fid, str)
                ]
                break

    if not parts.get("default_config_fields"):
        for s in parsed_steps:
            inst = (s.config or {}).get("install_fields") if s.config else None
            if inst and isinstance(inst, list):
                parts["default_config_fields"] = inst
                break
        if not parts.get("default_config_fields") and auth_type:
            if auth_type == "oauth2":
                parts["default_config_fields"] = [
                    {
                        "key": "client_id",
                        "label": "Client ID",
                        "type": "text",
                        "required": True,
                    },
                    {
                        "key": "client_secret",
                        "label": "Client Secret",
                        "type": "password",
                        "required": True,
                    },
                ]
            elif auth_type in ("api_key", "bearer"):
                parts["default_config_fields"] = [
                    {
                        "key": "api_key",
                        "label": "API Key",
                        "type": "password",
                        "required": True,
                    }
                ]


# ── Plan generation ───────────────────────────────────────────────────


async def generate_plan(
    session_id: str,
    tenant_id: str | None,
) -> dict[str, Any]:
    """Generate an integration plan for a session using Claude.

    Updates the session document in-place and returns the plan + metadata.
    """
    oid = ObjectId(session_id)
    session = await sessions_collection().find_one({"_id": oid})
    if not session:
        raise ValueError(f"Session {session_id} not found")

    provider = session["provider"]
    service = session["service"]
    user_prompt = session.get("user_prompt", "")
    connector_name = session.get("connector_name", "")

    # Always prefer the stored service_slug (includes unique hash from session creation).
    # Recomputing from connector_name/provider+service produces a plain slug (e.g. "google_gmail")
    # that differs from the stored one (e.g. "google_gmail_6750e5"), causing two R2 directories.
    _stored_slug = session.get("service_slug", "")
    if _stored_slug:
        service_slug = _stored_slug
    else:
        service_slug = _slug_from_connector_name(connector_name) if connector_name else _service_slug(provider, service)

    # Look up catalog metadata — fall back to a minimal synthetic entry for custom connectors
    catalog = get_service_detail(provider, service)
    if not catalog:
        # Custom connector: synthesise minimal catalog entry from session data
        connector_name_clean = connector_name or f"{provider.title()} {service.replace('_', ' ').title()}"
        catalog = {
            "provider": provider,
            "service": service,
            "service_key": service,
            "display_name": connector_name_clean,
            "description": session.get("user_prompt") or f"Custom connector for {connector_name_clean}",
            "auth_type": session.get("auth_type") or "api_key",
            "category": "custom",
        }

    # Fetch current guidelines — injected into prompt so the plan is always compliant
    guidelines_content, guidelines_version = await _get_guidelines_for_planning()

    # Build required_config_fields section if the catalog defines mandatory install fields
    _rcf = catalog.get("required_config_fields", [])
    if _rcf:
        _rcf_lines = "\n".join(
            f"  - `{f['key']}` ({f['label']}) — bind:{f.get('bind', False)} — {f.get('help', '')}" for f in _rcf
        )
        required_config_fields_section = (
            "- **Required Config Fields** — these MUST appear in `default_config_fields` "
            "exactly as listed (correct key, bind value, and help text):\n" + _rcf_lines + "\n"
        )
    else:
        required_config_fields_section = ""

    # Build selected_features_section from session
    _sel_features = session.get("selected_features", [])
    if _sel_features:
        _sel_section = (
            "**User-selected features** — the user has explicitly chosen these features. "
            "You MUST mark all of them as `recommended: true` in `recommended_features` and "
            "include all of their feature IDs in the `write_connector` step's `features` array:\n"
            + "\n".join(f"  - {fid}" for fid in _sel_features)
            + "\n\n"
        )
    else:
        _sel_section = ""

    # Build system prompt with service context + coding standards
    _prompt_base = await r2_service.get_step_prompt("PLANNING_SYSTEM_PROMPT", PLANNING_SYSTEM_PROMPT)
    system = _safe_format(
        _prompt_base,
        base_connector_interface=BASE_CONNECTOR_INTERFACE,
        connector_name=connector_name or catalog["display_name"],
        package_root=service_slug,
        provider=provider,
        service_name=catalog["display_name"],
        auth_type=catalog.get("auth_type", "unknown"),
        sdk_package=catalog.get("sdk_package", ""),
        docs_url=catalog.get("docs_url", ""),
        default_scopes=json.dumps(catalog.get("default_scopes", [])),
        required_config_fields_section=required_config_fields_section,
        selected_features_section=_sel_section,
        guidelines=guidelines_content,
        guidelines_version=guidelines_version,
    )

    # Phase 5: history may be an R2 pointer dict in Mongo — hydrate to the
    # real list before passing it through to the LLM context builder.
    prior_history = await hydrate_conversation_history(session=session)

    # Extract user-requested operations from raw prompt and inject as explicit method list
    extracted_ops = _extract_user_operations(session.get("user_prompt", "") or user_prompt or "", service_slug)
    if user_prompt:
        ops_block = ""
        if extracted_ops:
            ops_list = "\n".join(f'  - "{op}"' for op in extracted_ops)
            ops_block = (
                f"\n⚠️ EXTRACTED USER-REQUESTED METHODS — these MUST appear in `write_connector.config.methods`:\n"
                f"{ops_list}\n"
                f"Add these ON TOP OF the base abstract methods (install, authorize, health_check, sync).\n"
                f"Each is a standalone `public async def` method — NEVER fold into sync().\n"
            )
        user_msg = (
            f"Build a connector integration plan for {catalog['display_name']}.\n\n"
            f"User requirements: {user_prompt}\n"
            f"{ops_block}\n"
            f"Remember: respond with ONLY the raw JSON object. No explanation. No markdown. Start with `{{`."
        )
    else:
        user_msg = f"Build a standard connector integration plan for {catalog['display_name']}."

    logger.info(
        "plan.generating",
        session_id=session_id,
        provider=provider,
        service=service,
        service_slug=service_slug,
        guidelines_version=guidelines_version,
        extracted_ops=extracted_ops,
    )

    # Propagate tenant_id to LLM ContextVar (needed for MCP mode; empty string pre-login)
    set_llm_tenant_id(tenant_id or "")

    llm_result, updated_history = await _claude.chat(
        user_message=user_msg,
        system=system,
        prior_history=prior_history,
        parse_json=True,
    )
    parts = _extract_plan_parts(llm_result)

    # Always enforce the correct package root regardless of what the LLM returned
    if parts.get("package_structure") is None:
        parts["package_structure"] = {}
    parts["package_structure"]["root"] = f"{service_slug}_connector"

    steps = _parse_steps(parts["raw_steps"])
    if not steps:
        raise ValueError("LLM produced no valid steps")

    # Always ensure: setup_instructions → version_upgrade at the end
    _ensure_terminal_steps(steps)
    # Guard: ensure user-requested operations are in write_connector.config.methods
    _ensure_user_methods_in_write_connector(steps, session.get("user_prompt", ""), service_slug)
    # Backfill extras when LLM used old format (bare array)
    _fill_missing_plan_extras(parts, steps, auth_type=session.get("auth_type", ""))

    plan = PlanDocument(steps=steps, version=1)

    # Phase 4: full plan body lives in R2. Mongo keeps only the slim summary
    # (version + per-step {index, title, type, status}). The Builder + codegen
    # lazy-hydrate the full step config back from R2 when needed.
    _plan_for_mongo = await persist_plan(
        session_id=session_id,
        provider=provider,
        service_slug=service_slug,
        plan=plan.model_dump(),
    )

    # Phase 5: conversation history → R2; Mongo gets a tiny pointer dict.
    _history_pointer = await persist_conversation_history(
        session_id=session_id,
        provider=provider,
        service_slug=service_slug,
        history=updated_history,
    )

    # Persist to session (include package_structure + recommended_features + default_config_fields + history)
    update_fields: dict[str, Any] = {
        "plan": _plan_for_mongo,
        "status": SessionStatus.REVIEWING.value,
        "conversation_history": _history_pointer,
        "updated_at": __import__("datetime").datetime.utcnow(),
    }
    if parts["package_structure"]:
        update_fields["package_structure"] = parts["package_structure"]
    if parts["recommended_features"]:
        update_fields["recommended_features"] = parts["recommended_features"]
    if parts["default_config_fields"]:
        update_fields["default_config_fields"] = parts["default_config_fields"]

    await sessions_collection().update_one(
        {"_id": oid},
        {"$set": update_fields},
    )

    logger.info("plan.generated", session_id=session_id, step_count=len(steps))

    # Persist to R2 (best-effort — failure does not block response)
    await r2_service.save_prompt_and_plan(
        provider,
        service_slug,
        tenant_id,
        user_prompt,
        {
            "steps": plan.model_dump()["steps"],
            "version": 1,
            "package_structure": parts["package_structure"],
            "recommended_features": parts["recommended_features"],
            "default_config_fields": parts["default_config_fields"],
        },
        guidelines_version=guidelines_version,
    )

    # Ingest plan into connector KB so Gemini can query it during code generation
    try:
        from datetime import datetime

        from integration.services.knowledge_service import ingest_connector_docs
        from integration.services.r2_service import _build_plan_markdown

        plan_md = _build_plan_markdown(
            provider,
            service_slug,
            user_prompt,
            {
                "steps": plan.model_dump()["steps"],
                "version": 1,
                "package_structure": parts["package_structure"],
                "recommended_features": parts["recommended_features"],
            },
            datetime.now(UTC).isoformat(),
        )
        await ingest_connector_docs(
            content=plan_md,
            title=f"Integration Plan: {provider} / {service}",
            tenant_id=tenant_id,
            provider=provider,
            service=service,  # raw service name — must match what ingest_step_output uses
            session_id=session_id,
        )
        logger.info("plan.ingested_to_kb", session_id=session_id)
    except Exception as kb_err:
        logger.warning("plan.kb_ingest_failed", session_id=session_id, error=str(kb_err))

    return {
        **plan.model_dump(),
        "package_structure": parts["package_structure"],
        "recommended_features": parts["recommended_features"],
        "default_config_fields": parts["default_config_fields"],
    }


# ── Parse raw Claude output → import plan (local execution flow) ─────


async def parse_and_import_plan(
    session_id: str,
    tenant_id: str,
    raw_output: str,
) -> dict[str, Any]:
    """Parse raw Claude CLI output, extract JSON plan, and import it.

    Called by the Electron desktop app after local Claude CLI finishes
    generating a plan.  Does all parsing server-side using the same
    _extract_plan_parts + _parse_steps logic as the SSE flow.
    """
    # Strip ANSI escape codes (terminal color sequences from PTY output)
    import re as _re

    clean = _re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", raw_output)

    # Try to extract a JSON block — fenced (```json…```) or bare outermost object
    json_obj: Any = None
    fence_match = _re.search(r"```(?:json)?\s*([\s\S]*?)```", clean)
    if fence_match:
        with contextlib.suppress(json.JSONDecodeError):
            json_obj = json.loads(fence_match.group(1).strip())

    if json_obj is None:
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end > start:
            with contextlib.suppress(json.JSONDecodeError):
                json_obj = json.loads(clean[start : end + 1])

    if json_obj is None:
        raise ValueError("No valid JSON found in Claude output")

    parts = _extract_plan_parts(json_obj)

    # Delegate to import_cached_plan which handles MongoDB + R2 persistence
    return await import_cached_plan(
        session_id,
        tenant_id,
        {
            "steps": parts["raw_steps"],
            "version": json_obj.get("version", 1) if isinstance(json_obj, dict) else 1,
            "package_structure": parts["package_structure"],
            "recommended_features": parts["recommended_features"],
            "default_config_fields": parts["default_config_fields"],
        },
    )


# ── Planning prompt assembly (no LLM call) ──────────────────────────


async def get_planning_prompt_text(session_id: str, tenant_id: str) -> dict[str, Any]:
    """Assemble the planning system + user prompts without calling Claude.

    Used by the Electron desktop app to run Claude CLI locally for plan generation.
    Returns {"system_prompt": str, "user_message": str, "full_prompt": str}.
    """
    oid = ObjectId(session_id)
    session = await sessions_collection().find_one({"_id": oid})
    if not session:
        raise ValueError(f"Session {session_id} not found")

    provider = session["provider"]
    service = session["service"]
    user_prompt = session.get("user_prompt", "")
    connector_name = session.get("connector_name", "")

    _stored_slug = session.get("service_slug", "")
    if _stored_slug:
        service_slug = _stored_slug
    else:
        service_slug = _slug_from_connector_name(connector_name) if connector_name else _service_slug(provider, service)

    catalog = get_service_detail(provider, service)
    if not catalog:
        connector_name_clean = connector_name or f"{provider.title()} {service.replace('_', ' ').title()}"
        catalog = {
            "provider": provider,
            "service": service,
            "service_key": service,
            "display_name": connector_name_clean,
            "description": session.get("user_prompt") or f"Custom connector for {connector_name_clean}",
            "auth_type": session.get("auth_type") or "api_key",
            "category": "custom",
        }

    guidelines_content, guidelines_version = await _get_guidelines_for_planning()

    # NOTE: do NOT reconstruct/expand the prompt here. This function exists solely to
    # hand the assembled prompt to the desktop app's LOCAL Claude CLI, which expands the
    # requirements itself during planning. Calling reconstruct_user_prompt() would spawn
    # a redundant SERVER-SIDE Claude CLI subprocess (900s timeout) that blocks this
    # endpoint for seconds-to-minutes and makes "Generate Plan" look frozen. The local
    # planning run does the expansion — keep the raw user_prompt as-is.

    _rcf = catalog.get("required_config_fields", [])
    required_config_fields_section = (
        (
            "- **Required Config Fields** — these MUST appear in `default_config_fields` "
            "exactly as listed (correct key, bind value, and help text):\n"
            + "\n".join(
                f"  - `{f['key']}` ({f['label']}) — bind:{f.get('bind', False)} — {f.get('help', '')}" for f in _rcf
            )
            + "\n"
        )
        if _rcf
        else ""
    )

    _sel_features = session.get("selected_features", [])
    selected_features_section = (
        (
            "**User-selected features** — the user has explicitly chosen these features. "
            "You MUST mark all of them as `recommended: true` in `recommended_features` and "
            "include all of their feature IDs in the `write_connector` step's `features` array:\n"
            + "\n".join(f"  - {fid}" for fid in _sel_features)
            + "\n\n"
        )
        if _sel_features
        else ""
    )

    _prompt_base = await r2_service.get_step_prompt("PLANNING_SYSTEM_PROMPT", PLANNING_SYSTEM_PROMPT)
    system = _safe_format(
        _prompt_base,
        base_connector_interface=BASE_CONNECTOR_INTERFACE,
        connector_name=connector_name or catalog["display_name"],
        package_root=service_slug,
        provider=provider,
        service_name=catalog["display_name"],
        auth_type=catalog.get("auth_type", "unknown"),
        sdk_package=catalog.get("sdk_package", ""),
        docs_url=catalog.get("docs_url", ""),
        default_scopes=json.dumps(catalog.get("default_scopes", [])),
        required_config_fields_section=required_config_fields_section,
        selected_features_section=selected_features_section,
        guidelines=guidelines_content,
        guidelines_version=guidelines_version,
    )

    # Extract user-requested operations from the raw prompt stored on the session
    # (before reconstruction so we capture the original intent verbs)
    raw_user_prompt_for_ops = session.get("user_prompt", "")
    extracted_ops = _extract_user_operations(raw_user_prompt_for_ops, service_slug)

    if user_prompt:
        ops_block = ""
        if extracted_ops:
            ops_list = "\n".join(f'  - "{op}"' for op in extracted_ops)
            ops_block = (
                f"\n⚠️ EXTRACTED USER-REQUESTED METHODS — these MUST appear in `write_connector.config.methods`:\n"
                f"{ops_list}\n"
                f"Add these ON TOP OF the base abstract methods (install, authorize, health_check, sync).\n"
                f"Each is a standalone `public async def` method — NEVER fold into sync().\n"
            )
        user_msg = (
            f"Build a connector integration plan for {catalog['display_name']}.\n\n"
            f"User requirements: {user_prompt}\n"
            f"{ops_block}\n"
            f"Remember: respond with ONLY the raw JSON object. No explanation. No markdown. Start with `{{`."
        )
    else:
        user_msg = (
            f"Build a standard connector integration plan for {catalog['display_name']}.\n\n"
            f"Remember: respond with ONLY the raw JSON object. No explanation. No markdown. Start with `{{`."
        )

    full_prompt = f"{system}\n\n---\n\n{user_msg}"

    return {
        "system_prompt": system,
        "user_message": user_msg,
        "full_prompt": full_prompt,
        "session_id": session_id,
        "provider": provider,
        "service": service,
        "service_name": catalog["display_name"],
    }


# ── Streaming plan generation (SSE) ─────────────────────────────────


async def generate_plan_stream(
    session_id: str,
    tenant_id: str | None,
    new_prompt: str | None = None,
    app_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Generate a plan and yield SSE events with real-time logs.

    If new_prompt is provided (forced regeneration from UI):
      - Updates session.user_prompt to new_prompt
      - Clears the R2 plan cache (plan_generated=False) so future initial loads don't serve stale plan
      - Resets conversation history so Claude starts from scratch (no prior plan context)

    Events:
      planning_start   — session found, starting
      planning_log     — status/progress messages
      llm_calling      — about to call Claude
      llm_response     — got response, parsing
      step_parsed      — each step parsed from LLM output
      planning_complete — plan saved, done
      planning_error   — something went wrong
    """
    t0 = time.time()

    try:
        yield _sse(
            "planning_start",
            {
                "session_id": session_id,
                "message": "Initializing plan generation...",
                "timestamp": time.time(),
            },
        )

        oid = ObjectId(session_id)
        session = await sessions_collection().find_one({"_id": oid})
        if not session:
            yield _sse("planning_error", {"message": f"Session {session_id} not found"})
            return

        # Propagate tenant_id to LLM ContextVar (needed for MCP mode; empty string pre-login)
        set_llm_tenant_id(tenant_id or "")

        # Set R2 bucket context so save_prompt_and_plan writes to the correct per-installation
        # bucket ("shielva-agentic-app-{app_id}").  app_id comes from the query param (SSE
        # EventSource can't send custom headers) or falls back to the session document.
        # Same fix pattern as execute_plan / ws_routes.py / sync-to-r2.
        _plan_app_id = (app_id or session.get("app_id") or "").strip()
        _plan_tenant_name = (session.get("tenant_name") or tenant_id or "").strip().lower()
        if _plan_app_id:
            r2_service._app_bucket_ctx.set(r2_service.app_id_to_bucket(_plan_app_id))
        if _plan_tenant_name:
            r2_service._tenant_bucket_ctx.set(_plan_tenant_name)

        provider = session["provider"]
        service = session["service"]
        connector_name = session.get("connector_name", "")

        # Always prefer the stored service_slug (includes unique hash from session creation).
        # Recomputing from connector_name/provider+service produces a plain slug (e.g. "google_gmail")
        # that differs from the stored one (e.g. "google_gmail_6750e5"), causing two R2 directories.
        # Must be computed before the `if new_prompt` block (invalidate_stale_plan uses it).
        _stored_slug = session.get("service_slug", "")
        if _stored_slug:
            service_slug = _stored_slug
        else:
            service_slug = (
                _slug_from_connector_name(connector_name) if connector_name else _service_slug(provider, service)
            )

        # If a new prompt was supplied (forced fresh regeneration), persist it and wipe R2 cache
        if new_prompt is not None:
            user_prompt = new_prompt.strip()
            await sessions_collection().update_one(
                {"_id": oid},
                {
                    "$set": {
                        "user_prompt": user_prompt,
                        "conversation_history": [],  # reset history so Claude starts fresh
                        "updated_at": __import__("datetime").datetime.utcnow(),
                    }
                },
            )
            session["conversation_history"] = []
            # Invalidate R2 so the next "Generate Plan" call doesn't serve the old cached plan
            try:
                await r2_service.invalidate_stale_plan(provider, service_slug, tenant_id)
            except Exception as _r2_err:
                pass  # best-effort — don't block on R2 failure
            yield _sse(
                "planning_log",
                {
                    "level": "info",
                    "message": "New prompt saved — generating fresh plan from scratch (R2 cache cleared)...",
                    "elapsed_ms": int((time.time() - t0) * 1000),
                },
            )
        else:
            user_prompt = session.get("user_prompt", "")

        yield _sse(
            "planning_log",
            {
                "level": "info",
                "message": f"Session loaded — provider={provider}, service={service}, package_root={service_slug}_connector",
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        # Look up catalog metadata — fall back to synthetic entry for custom connectors
        catalog = get_service_detail(provider, service)
        if not catalog:
            connector_name_clean = connector_name or f"{provider.title()} {service.replace('_', ' ').title()}"
            catalog = {
                "provider": provider,
                "service": service,
                "service_key": service,
                "display_name": connector_name_clean,
                "description": session.get("user_prompt") or f"Custom connector for {connector_name_clean}",
                "auth_type": session.get("auth_type") or "api_key",
                "category": "custom",
            }
            yield _sse(
                "planning_log",
                {
                    "level": "info",
                    "message": f"Custom connector — using synthetic catalog entry for {connector_name_clean}",
                    "elapsed_ms": int((time.time() - t0) * 1000),
                },
            )
        else:
            yield _sse(
                "planning_log",
                {
                    "level": "info",
                    "message": f"Catalog entry found: {catalog['display_name']} (auth={catalog.get('auth_type', '?')}, sdk={catalog.get('sdk_package', 'N/A')})",
                    "elapsed_ms": int((time.time() - t0) * 1000),
                },
            )

        # Fetch current guidelines — injected into prompt so plan is always compliant
        guidelines_content, guidelines_version = await _get_guidelines_for_planning()
        yield _sse(
            "planning_log",
            {
                "level": "info",
                "message": f"CODE_EXECUTION_GUIDELINES loaded (version={guidelines_version}, {len(guidelines_content)} chars). Injecting into planning prompt.",
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        # Build required_config_fields section for stream prompt
        _rcf_stream = catalog.get("required_config_fields", [])
        if _rcf_stream:
            _rcf_lines_s = "\n".join(
                f"  - `{f['key']}` ({f['label']}) — bind:{f.get('bind', False)} — {f.get('help', '')}"
                for f in _rcf_stream
            )
            _rcf_section_stream = (
                "- **Required Config Fields** — these MUST appear in `default_config_fields` "
                "exactly as listed (correct key, bind value, and help text):\n" + _rcf_lines_s + "\n"
            )
        else:
            _rcf_section_stream = ""

        # Build selected_features_section from session
        _sel_features_stream = session.get("selected_features", [])
        if _sel_features_stream:
            _sel_section_stream = (
                "**User-selected features** — the user has explicitly chosen these features. "
                "You MUST mark all of them as `recommended: true` in `recommended_features` and "
                "include all of their feature IDs in the `write_connector` step's `features` array:\n"
                + "\n".join(f"  - {fid}" for fid in _sel_features_stream)
                + "\n\n"
            )
        else:
            _sel_section_stream = ""

        # Build system prompt with coding standards injected
        _prompt_base_stream = await r2_service.get_step_prompt("PLANNING_SYSTEM_PROMPT", PLANNING_SYSTEM_PROMPT)
        system = _safe_format(
            _prompt_base_stream,
            base_connector_interface=BASE_CONNECTOR_INTERFACE,
            connector_name=connector_name or catalog["display_name"],
            package_root=service_slug,
            provider=provider,
            service_name=catalog["display_name"],
            auth_type=catalog.get("auth_type", "unknown"),
            sdk_package=catalog.get("sdk_package", ""),
            docs_url=catalog.get("docs_url", ""),
            default_scopes=json.dumps(catalog.get("default_scopes", [])),
            required_config_fields_section=_rcf_section_stream,
            selected_features_section=_sel_section_stream,
            guidelines=guidelines_content,
            guidelines_version=guidelines_version,
        )

        # Phase 5: hydrate the R2 pointer back into the full conversation list.
        prior_history = await hydrate_conversation_history(session=session)

        # Extract user-requested operations and inject as explicit method list
        _stream_extracted_ops = _extract_user_operations(
            session.get("user_prompt", "") or user_prompt or "", service_slug
        )
        if user_prompt:
            _ops_block = ""
            if _stream_extracted_ops:
                _ops_list = "\n".join(f'  - "{op}"' for op in _stream_extracted_ops)
                _ops_block = (
                    f"\n⚠️ EXTRACTED USER-REQUESTED METHODS — these MUST appear in `write_connector.config.methods`:\n"
                    f"{_ops_list}\n"
                    f"Add these ON TOP OF the base abstract methods (install, authorize, health_check, sync).\n"
                    f"Each is a standalone `public async def` method — NEVER fold into sync().\n"
                )
            user_msg = (
                f"Build a connector integration plan for {catalog['display_name']}.\n\n"
                f"User requirements: {user_prompt}\n"
                f"{_ops_block}\n"
                f"Remember: respond with ONLY the raw JSON object. No explanation. No markdown. Start with `{{`."
            )
        else:
            user_msg = f"Build a standard connector integration plan for {catalog['display_name']}."

        yield _sse(
            "planning_log",
            {
                "level": "info",
                "message": (
                    f"System prompt built ({len(system)} chars). "
                    f"Injected BaseConnector interface + catalog metadata. "
                    f"Extracted user operations: {_stream_extracted_ops}. "
                    f"Prior conversation turns: {len(prior_history) // 2}."
                ),
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        yield _sse(
            "llm_calling",
            {
                "message": "Calling Claude AI — generating integration plan...",
                "prompt_length": len(system) + len(user_msg),
                "prior_turns": len(prior_history) // 2,
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        # Stream Claude tokens via asyncio queue so we can yield SSE events
        # while the LLM call is running concurrently.
        _token_queue: asyncio.Queue[str] = asyncio.Queue()

        async def _on_token(_chars: int, chunk: str) -> None:
            await _token_queue.put(chunk)

        async def _run_chat():
            return await _claude.chat(
                user_message=user_msg,
                system=system,
                prior_history=prior_history,
                parse_json=True,
                on_chunk=_on_token,
            )

        _chat_task = asyncio.create_task(_run_chat())

        # Drain token queue while chat task runs — yield each chunk as llm_token SSE
        while True:
            try:
                _chunk = await asyncio.wait_for(_token_queue.get(), timeout=0.05)
                yield _sse("llm_token", {"chunk": _chunk})
            except TimeoutError:
                if _chat_task.done():
                    while not _token_queue.empty():
                        _chunk = _token_queue.get_nowait()
                        yield _sse("llm_token", {"chunk": _chunk})
                    break

        llm_result, updated_history = await _chat_task

        yield _sse(
            "llm_response",
            {
                "message": "Claude response received. Parsing plan data...",
                "response_type": type(llm_result).__name__,
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        parts = _extract_plan_parts(llm_result)
        raw_steps = parts["raw_steps"]
        recommended_features = parts["recommended_features"]
        default_config_fields = parts["default_config_fields"]

        # Always enforce the correct package root regardless of what the LLM returned
        if parts.get("package_structure") is None:
            parts["package_structure"] = {}
        parts["package_structure"]["root"] = f"{service_slug}_connector"
        package_structure = parts["package_structure"]

        if package_structure:
            yield _sse(
                "planning_log",
                {
                    "level": "info",
                    "message": f"Package structure: {package_structure.get('root', '?')} — {len(package_structure.get('files', []))} files",
                    "elapsed_ms": int((time.time() - t0) * 1000),
                },
            )

        if recommended_features:
            feat_names = [f.get("label", f.get("id", "?")) for f in recommended_features]
            yield _sse(
                "planning_log",
                {
                    "level": "info",
                    "message": f"Recommended features: {', '.join(feat_names)}",
                    "elapsed_ms": int((time.time() - t0) * 1000),
                },
            )

        if default_config_fields:
            field_names = [f.get("label", f.get("key", "?")) for f in default_config_fields]
            yield _sse(
                "planning_log",
                {
                    "level": "info",
                    "message": f"Config fields ({len(default_config_fields)}): {', '.join(field_names)}",
                    "elapsed_ms": int((time.time() - t0) * 1000),
                },
            )

        yield _sse(
            "planning_log",
            {
                "level": "info",
                "message": f"LLM returned {len(raw_steps)} raw steps. Validating...",
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        # Parse steps, emit each one
        steps = []
        for i, raw in enumerate(raw_steps):
            step_type = raw.get("type", "")
            if step_type not in VALID_STEP_TYPES:
                yield _sse(
                    "planning_log",
                    {
                        "level": "warn",
                        "message": f"Skipping invalid step type '{step_type}' at index {i}",
                        "elapsed_ms": int((time.time() - t0) * 1000),
                    },
                )
                continue
            step = PlanStep(
                index=len(steps),
                type=StepType(step_type),
                title=raw.get("title", f"Step {i + 1}"),
                description=raw.get("description", ""),
                estimated_duration_s=raw.get("estimated_duration_s", 30),
                config=raw.get("config", {}),
                status=StepStatus.PENDING,
            )
            steps.append(step)
            yield _sse(
                "step_parsed",
                {
                    "index": step.index,
                    "type": step_type,
                    "title": step.title,
                    "description": step.description[:120],
                    "elapsed_ms": int((time.time() - t0) * 1000),
                },
            )

        if not steps:
            yield _sse("planning_error", {"message": "LLM produced no valid steps"})
            return

        # Enforce terminal step sequence: setup_instructions → version_upgrade
        _ensure_terminal_steps(steps)
        # Guard: inject any user-requested methods the LLM missed
        _ensure_user_methods_in_write_connector(steps, session.get("user_prompt", ""), service_slug)
        # Backfill extras when LLM used old format (bare array)
        _stream_parts = {
            "recommended_features": recommended_features,
            "default_config_fields": default_config_fields,
        }
        _fill_missing_plan_extras(_stream_parts, steps, auth_type=session.get("auth_type", ""))
        recommended_features = _stream_parts["recommended_features"]
        default_config_fields = _stream_parts["default_config_fields"]

        plan = PlanDocument(steps=steps, version=1)

        yield _sse(
            "planning_log",
            {
                "level": "info",
                "message": f"Validated {len(steps)} steps. Saving plan to database...",
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        # Phase 4: full plan → R2; slim summary → Mongo (see persist_plan docstring).
        _plan_for_mongo = await persist_plan(
            session_id=session_id,
            provider=provider,
            service_slug=service_slug,
            plan=plan.model_dump(),
        )
        # Phase 5: conversation history → R2; Mongo gets a pointer.
        _history_pointer = await persist_conversation_history(
            session_id=session_id,
            provider=provider,
            service_slug=service_slug,
            history=updated_history,
        )

        # Persist plan + conversation history
        update_fields: dict[str, Any] = {
            "plan": _plan_for_mongo,
            "status": SessionStatus.REVIEWING.value,
            "conversation_history": _history_pointer,
            "updated_at": __import__("datetime").datetime.utcnow(),
        }
        if package_structure:
            update_fields["package_structure"] = package_structure
        if recommended_features:
            update_fields["recommended_features"] = recommended_features
        if default_config_fields:
            update_fields["default_config_fields"] = default_config_fields

        await sessions_collection().update_one(
            {"_id": oid},
            {"$set": update_fields},
        )

        # Persist to R2 (best-effort — failure does not interrupt SSE stream)
        yield _sse(
            "planning_log",
            {
                "level": "info",
                "message": "Saving plan to Cloudflare R2 (prompts.csv + plan.json + plan.md)...",
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )
        try:
            await r2_service.save_prompt_and_plan(
                provider,
                service_slug,
                tenant_id,
                user_prompt,
                {
                    "steps": plan.model_dump()["steps"],
                    "version": 1,
                    "package_structure": package_structure,
                    "recommended_features": recommended_features,
                    "default_config_fields": default_config_fields,
                },
                guidelines_version=guidelines_version,
            )
            yield _sse(
                "planning_log",
                {
                    "level": "success",
                    "message": f"R2 saved — {settings.R2_BUCKET_NAME}/{provider}/{service}/{tenant_id}/plan.md (guidelines v{guidelines_version})",
                    "elapsed_ms": int((time.time() - t0) * 1000),
                },
            )
        except Exception as r2_exc:
            yield _sse(
                "planning_log",
                {
                    "level": "warn",
                    "message": f"R2 save skipped: {r2_exc}",
                    "elapsed_ms": int((time.time() - t0) * 1000),
                },
            )

        total_ms = int((time.time() - t0) * 1000)
        yield _sse(
            "planning_complete",
            {
                "message": f"Plan generated successfully — {len(steps)} steps in {total_ms}ms",
                "step_count": len(steps),
                "version": 1,
                "elapsed_ms": total_ms,
                "plan": {
                    **plan.model_dump(),
                    "package_structure": package_structure,
                    "recommended_features": recommended_features,
                    "default_config_fields": default_config_fields,
                },
            },
        )

        logger.info(
            "plan.stream_generated",
            session_id=session_id,
            step_count=len(steps),
            elapsed_ms=total_ms,
        )

    except Exception as exc:
        logger.error("plan.stream_error", session_id=session_id, error=str(exc), exc_info=True)
        yield _sse(
            "planning_error",
            {
                "message": str(exc),
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )


# ── Replan ────────────────────────────────────────────────────────────


async def replan(
    session_id: str,
    tenant_id: str | None,
    step_index: int,
    user_comment: str,
) -> dict[str, Any]:
    """Regenerate the plan incorporating user feedback on a specific step."""
    oid = ObjectId(session_id)
    session = await sessions_collection().find_one({"_id": oid})
    if not session:
        raise ValueError(f"Session {session_id} not found")

    provider = session["provider"]
    service = session["service"]
    connector_name = session.get("connector_name", "")
    current_plan = session.get("plan", {})
    current_version = current_plan.get("version", 1)

    # Always prefer the stored service_slug (includes unique hash from session creation).
    _stored_slug = session.get("service_slug", "")
    if _stored_slug:
        service_slug = _stored_slug
    else:
        service_slug = _slug_from_connector_name(connector_name) if connector_name else _service_slug(provider, service)

    catalog = get_service_detail(provider, service)
    if not catalog:
        connector_name_clean = connector_name or f"{provider.title()} {service.replace('_', ' ').title()}"
        catalog = {
            "provider": provider,
            "service": service,
            "service_key": service,
            "display_name": connector_name_clean,
            "description": session.get("user_prompt") or f"Custom connector for {connector_name_clean}",
            "auth_type": session.get("auth_type") or "api_key",
            "category": "custom",
        }

    # Build current plan JSON including extras for context
    current_data = {
        "steps": current_plan.get("steps", []),
        "package_structure": session.get("package_structure"),
        "recommended_features": session.get("recommended_features", []),
    }

    # Fetch current guidelines
    guidelines_content, guidelines_version = await _get_guidelines_for_planning()

    _replan_base = await r2_service.get_step_prompt("REPLAN_SYSTEM_PROMPT", REPLAN_SYSTEM_PROMPT)
    system = _safe_format(
        _replan_base,
        base_connector_interface=BASE_CONNECTOR_INTERFACE,
        connector_name=connector_name or catalog["display_name"],
        package_root=service_slug,
        provider=provider,
        service_name=catalog["display_name"],
        auth_type=catalog.get("auth_type", "unknown"),
        sdk_package=catalog.get("sdk_package", ""),
        current_plan_json=json.dumps(current_data, indent=2),
        step_index=step_index,
        user_comment=user_comment,
        guidelines=guidelines_content,
        guidelines_version=guidelines_version,
    )

    # Load prior conversation history so Claude recalls the full planning journey
    # Phase 5: hydrate from R2 so the replan request has the full history.
    prior_history = await hydrate_conversation_history(session=session)
    user_msg = f"Replan the integration. My feedback on step {step_index}: {user_comment}"

    logger.info(
        "plan.replanning",
        session_id=session_id,
        step_index=step_index,
        prior_turns=len(prior_history) // 2,
    )

    llm_result, updated_history = await _claude.chat(
        user_message=user_msg,
        system=system,
        prior_history=prior_history,
        parse_json=True,
    )
    parts = _extract_plan_parts(llm_result)

    # Always enforce the correct package root regardless of what the LLM returned
    if parts.get("package_structure") is None:
        parts["package_structure"] = {}
    parts["package_structure"]["root"] = f"{service_slug}_connector"

    steps = _parse_steps(parts["raw_steps"])
    if not steps:
        raise ValueError("LLM produced no valid steps during replan")

    # Enforce terminal step sequence: setup_instructions → version_upgrade
    _ensure_terminal_steps(steps)
    # Guard: inject any user-requested methods the LLM missed
    _ensure_user_methods_in_write_connector(steps, session.get("user_prompt", ""), service_slug)

    new_plan = PlanDocument(steps=steps, version=current_version + 1)

    # Phase 4: full plan → R2; slim summary → Mongo.
    _plan_for_mongo = await persist_plan(
        session_id=session_id,
        provider=provider,
        service_slug=service_slug,
        plan=new_plan.model_dump(),
    )
    _history_pointer = await persist_conversation_history(
        session_id=session_id,
        provider=provider,
        service_slug=service_slug,
        history=updated_history,
    )

    # Save comment + updated plan + any new package_structure/features/config_fields + conversation history
    comment = StepComment(step_index=step_index, comment=user_comment)
    update_fields: dict[str, Any] = {
        "plan": _plan_for_mongo,
        "status": SessionStatus.REVIEWING.value,
        "conversation_history": _history_pointer,
        "updated_at": __import__("datetime").datetime.utcnow(),
    }
    if parts["package_structure"]:
        update_fields["package_structure"] = parts["package_structure"]
    if parts["recommended_features"]:
        update_fields["recommended_features"] = parts["recommended_features"]
    if parts["default_config_fields"]:
        update_fields["default_config_fields"] = parts["default_config_fields"]

    await sessions_collection().update_one(
        {"_id": oid},
        {
            "$set": update_fields,
            "$push": {"comments": comment.model_dump()},
        },
    )

    logger.info(
        "plan.replanned",
        session_id=session_id,
        version=new_plan.version,
        step_count=len(steps),
    )

    # Persist to R2 (best-effort)
    user_prompt = session.get("user_prompt", "")
    await r2_service.save_prompt_and_plan(
        provider,
        service_slug,
        tenant_id,
        f"{user_prompt} [replan v{new_plan.version}]: {user_comment}",
        {
            "steps": new_plan.model_dump()["steps"],
            "version": new_plan.version,
            "package_structure": parts["package_structure"],
            "recommended_features": parts["recommended_features"],
            "default_config_fields": parts["default_config_fields"],
        },
        guidelines_version=guidelines_version,
    )

    return {
        **new_plan.model_dump(),
        "package_structure": parts["package_structure"],
        "recommended_features": parts["recommended_features"],
        "default_config_fields": parts["default_config_fields"],
    }


# ── Replan with streaming SSE logs ────────────────────────────────────


async def replan_stream(
    session_id: str,
    tenant_id: str | None,
    step_index: int,
    user_comment: str,
    app_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Regenerate the plan with user feedback — streams SSE events for real-time UI logs."""
    t0 = time.time()
    try:
        yield _sse(
            "replan_start",
            {
                "session_id": session_id,
                "message": "Starting plan regeneration...",
                "step_index": step_index,
                "timestamp": t0,
            },
        )

        oid = ObjectId(session_id)
        session = await sessions_collection().find_one({"_id": oid})
        if not session:
            yield _sse("replan_error", {"message": f"Session {session_id} not found"})
            return

        # Propagate tenant_id to LLM ContextVar (needed for MCP mode; empty string pre-login)
        set_llm_tenant_id(tenant_id or "")

        # Set R2 bucket context — same fix as generate_plan_stream / execute_plan / sync-to-r2.
        _replan_app_id = (app_id or session.get("app_id") or "").strip()
        _replan_tenant_name = (session.get("tenant_name") or tenant_id or "").strip().lower()
        if _replan_app_id:
            r2_service._app_bucket_ctx.set(r2_service.app_id_to_bucket(_replan_app_id))
        if _replan_tenant_name:
            r2_service._tenant_bucket_ctx.set(_replan_tenant_name)

        provider = session["provider"]
        service = session["service"]
        connector_name = session.get("connector_name", "")
        current_plan = session.get("plan", {})
        current_version = current_plan.get("version", 1)

        # Always prefer the stored service_slug (includes unique hash from session creation).
        _stored_slug = session.get("service_slug", "")
        if _stored_slug:
            service_slug = _stored_slug
        else:
            service_slug = (
                _slug_from_connector_name(connector_name) if connector_name else _service_slug(provider, service)
            )

        yield _sse(
            "replan_log",
            {
                "level": "info",
                "message": f"Session loaded — {provider}/{service} (plan v{current_version}, package_root={service_slug}_connector)",
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        catalog = get_service_detail(provider, service)
        if not catalog:
            connector_name_clean = connector_name or f"{provider.title()} {service.replace('_', ' ').title()}"
            catalog = {
                "provider": provider,
                "service": service,
                "service_key": service,
                "display_name": connector_name_clean,
                "description": session.get("user_prompt") or f"Custom connector for {connector_name_clean}",
                "auth_type": session.get("auth_type") or "api_key",
                "category": "custom",
            }

        # Fetch current guidelines
        guidelines_content, guidelines_version = await _get_guidelines_for_planning()
        yield _sse(
            "replan_log",
            {
                "level": "info",
                "message": f"Guidelines loaded (version={guidelines_version}). Building replan prompt...",
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        current_data = {
            "steps": current_plan.get("steps", []),
            "package_structure": session.get("package_structure"),
            "recommended_features": session.get("recommended_features", []),
        }

        _replan_base_stream = await r2_service.get_step_prompt("REPLAN_SYSTEM_PROMPT", REPLAN_SYSTEM_PROMPT)
        system = _safe_format(
            _replan_base_stream,
            base_connector_interface=BASE_CONNECTOR_INTERFACE,
            connector_name=connector_name or catalog["display_name"],
            package_root=service_slug,
            provider=provider,
            service_name=catalog["display_name"],
            auth_type=catalog.get("auth_type", "unknown"),
            sdk_package=catalog.get("sdk_package", ""),
            current_plan_json=json.dumps(current_data, indent=2),
            step_index=step_index,
            user_comment=user_comment,
            guidelines=guidelines_content,
            guidelines_version=guidelines_version,
        )

        # Phase 5: hydrate from R2 so the replan request has the full history.
        prior_history = await hydrate_conversation_history(session=session)
        user_msg = f"Replan the integration. My feedback on step {step_index}: {user_comment}"

        yield _sse(
            "llm_calling",
            {
                "message": f"Calling Claude AI — regenerating plan with your feedback (prior turns: {len(prior_history) // 2})...",
                "prompt_length": len(system) + len(user_msg),
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        llm_result, updated_history = await _claude.chat(
            user_message=user_msg,
            system=system,
            prior_history=prior_history,
            parse_json=True,
        )

        yield _sse(
            "llm_response",
            {
                "message": "Claude response received. Parsing updated plan...",
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        parts = _extract_plan_parts(llm_result)

        # Always enforce the correct package root
        if parts.get("package_structure") is None:
            parts["package_structure"] = {}
        parts["package_structure"]["root"] = f"{service_slug}_connector"

        steps = _parse_steps(parts["raw_steps"])
        if not steps:
            yield _sse("replan_error", {"message": "LLM produced no valid steps during replan"})
            return

        # Emit each parsed step
        for step in steps:
            yield _sse(
                "step_parsed",
                {
                    "index": step.index,
                    "type": step.type.value,
                    "title": step.title,
                    "elapsed_ms": int((time.time() - t0) * 1000),
                },
            )

        # Enforce terminal step sequence: setup_instructions → version_upgrade
        _ensure_terminal_steps(steps)
        _ensure_user_methods_in_write_connector(steps, session.get("user_prompt", ""), service_slug)

        new_plan = PlanDocument(steps=steps, version=current_version + 1)

        # Phase 4: full plan → R2; slim summary → Mongo.
        _plan_for_mongo = await persist_plan(
            session_id=session_id,
            provider=provider,
            service_slug=service_slug,
            plan=new_plan.model_dump(),
        )
        _history_pointer = await persist_conversation_history(
            session_id=session_id,
            provider=provider,
            service_slug=service_slug,
            history=updated_history,
        )

        # Persist
        comment_obj = StepComment(step_index=step_index, comment=user_comment)
        update_fields: dict[str, Any] = {
            "plan": _plan_for_mongo,
            "status": SessionStatus.REVIEWING.value,
            "conversation_history": _history_pointer,
            "updated_at": __import__("datetime").datetime.utcnow(),
        }
        if parts["package_structure"]:
            update_fields["package_structure"] = parts["package_structure"]
        if parts["recommended_features"]:
            update_fields["recommended_features"] = parts["recommended_features"]
        if parts["default_config_fields"]:
            update_fields["default_config_fields"] = parts["default_config_fields"]

        await sessions_collection().update_one(
            {"_id": oid},
            {"$set": update_fields, "$push": {"comments": comment_obj.model_dump()}},
        )

        yield _sse(
            "replan_log",
            {
                "level": "success",
                "message": f"Plan saved to database (v{new_plan.version}). Updating R2 cache...",
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )

        # Persist to R2 (best-effort)
        user_prompt = session.get("user_prompt", "")
        await r2_service.save_prompt_and_plan(
            provider,
            service_slug,
            tenant_id,
            f"{user_prompt} [replan v{new_plan.version}]: {user_comment}",
            {
                "steps": new_plan.model_dump()["steps"],
                "version": new_plan.version,
                "package_structure": parts["package_structure"],
                "recommended_features": parts["recommended_features"],
                "default_config_fields": parts["default_config_fields"],
            },
            guidelines_version=guidelines_version,
        )

        plan_result = {
            **new_plan.model_dump(),
            "package_structure": parts["package_structure"],
            "recommended_features": parts["recommended_features"],
            "default_config_fields": parts["default_config_fields"],
        }

        yield _sse(
            "replan_complete",
            {
                "message": f"Plan regenerated successfully — {len(steps)} steps (v{new_plan.version})",
                "version": new_plan.version,
                "step_count": len(steps),
                "elapsed_ms": int((time.time() - t0) * 1000),
                "plan": plan_result,
            },
        )

    except Exception as exc:
        logger.error(
            "plan.replan_stream_failed",
            session_id=session_id,
            error=str(exc),
            exc_info=True,
        )
        yield _sse(
            "replan_error",
            {
                "message": str(exc),
                "elapsed_ms": int((time.time() - t0) * 1000),
            },
        )


# ── Approve ───────────────────────────────────────────────────────────


async def approve_plan(session_id: str, tenant_id: str | None) -> dict[str, Any]:
    """Mark all steps as approved and transition session to APPROVED."""
    oid = ObjectId(session_id)
    session = await sessions_collection().find_one({"_id": oid})
    if not session:
        raise ValueError(f"Session {session_id} not found")

    plan = session.get("plan", {})
    steps = plan.get("steps", [])
    for step in steps:
        step["status"] = StepStatus.APPROVED.value

    await sessions_collection().update_one(
        {"_id": oid},
        {
            "$set": {
                "plan.steps": steps,
                "status": SessionStatus.APPROVED.value,
                "updated_at": __import__("datetime").datetime.utcnow(),
            }
        },
    )

    logger.info("plan.approved", session_id=session_id, step_count=len(steps))

    # Update approval status in R2 progress.json (best-effort)
    provider = session.get("provider", "")
    service = session.get("service", "")
    _connector_name = session.get("connector_name", "")
    # Always prefer the stored service_slug (includes unique hash from session creation).
    _stored_approval_slug = session.get("service_slug", "")
    _approval_slug = (
        _stored_approval_slug
        if _stored_approval_slug
        else (_slug_from_connector_name(_connector_name) if _connector_name else _service_slug(provider, service))
    )
    if provider and _approval_slug:
        await r2_service.update_approval_status(provider, _approval_slug, tenant_id, "approved")

    return {"approved": True, "step_count": len(steps)}


# ── Refresh R2 plan to current guidelines ────────────────────────────


async def refresh_r2_plan(session_id: str, tenant_id: str) -> dict[str, Any]:
    """Regenerate the plan for a session using the current CODE_EXECUTION_GUIDELINES
    and overwrite the R2 cache (plan.json + plan.md + progress.json).

    Called on re-execute so the cached plan always reflects the latest standards
    before execution begins.  Returns the refreshed plan data.

    Optimization: if the cached plan's guidelines_version matches the current
    version, skip the LLM call entirely and import the cached plan directly.
    """
    logger.info("plan.refresh_r2_start", session_id=session_id, tenant_id=tenant_id)

    # Step 1: fetch session to resolve provider / service_slug (needed for R2 key)
    oid = ObjectId(session_id)
    session = await sessions_collection().find_one({"_id": oid})
    if not session:
        raise ValueError(f"Session {session_id} not found")

    provider = session["provider"]
    service = session["service"]
    connector_name = session.get("connector_name", "")
    # Always prefer the stored service_slug (includes unique hash from session creation).
    _stored_slug = session.get("service_slug", "")
    if _stored_slug:
        service_slug = _stored_slug
    else:
        service_slug = _slug_from_connector_name(connector_name) if connector_name else _service_slug(provider, service)

    # Step 2: fetch R2 cache and current guidelines version in parallel
    cached, (_, current_version) = await asyncio.gather(
        r2_service.get_history(provider, service_slug, tenant_id),
        _get_guidelines_for_planning(),
    )

    # Step 3: cache hit — same guidelines version, skip LLM
    if (
        cached
        and cached.get("plan")
        and current_version != "unknown"
        and cached.get("guidelines_version", "unknown") == current_version
    ):
        logger.info(
            "plan.refresh_r2_cache_hit",
            session_id=session_id,
            guidelines_version=current_version,
        )
        return await import_cached_plan(session_id, tenant_id, cached["plan"])

    # Step 4: stale or missing cache — full LLM regeneration
    logger.info(
        "plan.refresh_r2_cache_miss",
        session_id=session_id,
        cached_version=cached.get("guidelines_version") if cached else None,
        current_version=current_version,
    )
    plan_data = await generate_plan(session_id, tenant_id)
    logger.info(
        "plan.refresh_r2_complete",
        session_id=session_id,
        step_count=len(plan_data.get("steps", [])),
    )
    return plan_data


# ── Import cached plan (skip LLM) ─────────────────────────────────────


async def import_cached_plan(
    session_id: str,
    tenant_id: str,
    plan_data: dict[str, Any],
) -> dict[str, Any]:
    """Import a pre-cached plan into a session without running the LLM.

    Saves to MongoDB (session doc) + R2/local dir cache (as source of truth)
    so the plan is always persisted identically to a backend-generated plan.
    Sets session status to REVIEWING so the wizard can continue normally.
    """
    oid = ObjectId(session_id)
    session = await sessions_collection().find_one({"_id": oid})
    if not session:
        raise ValueError(f"Session {session_id} not found")

    provider = session["provider"]
    service = session["service"]
    connector_name = session.get("connector_name", "")
    user_prompt = session.get("user_prompt", "")

    # Always prefer the stored service_slug (includes unique hash from session creation).
    _stored_slug = session.get("service_slug", "")
    if _stored_slug:
        service_slug = _stored_slug
    else:
        service_slug = _slug_from_connector_name(connector_name) if connector_name else _service_slug(provider, service)

    raw_steps = plan_data.get("steps", [])
    steps = _parse_steps(raw_steps)
    if not steps:
        raise ValueError("Cached plan contains no valid steps")

    # Enforce terminal step sequence: setup_instructions → version_upgrade
    _ensure_terminal_steps(steps)
    _ensure_user_methods_in_write_connector(steps, user_prompt, service_slug)

    # Always set correct package root
    package_structure = plan_data.get("package_structure") or {}
    package_structure["root"] = f"{service_slug}_connector"
    recommended_features = plan_data.get("recommended_features") or []
    default_config_fields = plan_data.get("default_config_fields") or []

    # When Claude used old format (bare array), derive extras from the plan steps and session
    if not recommended_features:
        for s in steps:
            feat_list = (s.config or {}).get("features") if s.config else None
            if feat_list and isinstance(feat_list, list):
                recommended_features = [
                    {
                        "id": fid,
                        "label": fid.replace("_", " ").title(),
                        "recommended": True,
                        "category": "connector",
                        "description": "",
                    }
                    for fid in feat_list
                    if isinstance(fid, str)
                ]
                break

    if not default_config_fields:
        for s in steps:
            inst_fields = (s.config or {}).get("install_fields") if s.config else None
            if inst_fields and isinstance(inst_fields, list):
                default_config_fields = inst_fields
                break
        if not default_config_fields:
            auth_type = session.get("auth_type", "")
            if auth_type == "oauth2":
                default_config_fields = [
                    {
                        "key": "client_id",
                        "label": "Client ID",
                        "type": "text",
                        "required": True,
                    },
                    {
                        "key": "client_secret",
                        "label": "Client Secret",
                        "type": "password",
                        "required": True,
                    },
                ]
            elif auth_type in ("api_key", "bearer"):
                default_config_fields = [
                    {
                        "key": "api_key",
                        "label": "API Key",
                        "type": "password",
                        "required": True,
                    }
                ]

    plan = PlanDocument(steps=steps, version=plan_data.get("version", 1))

    # Phase 4: full plan → R2; slim summary → Mongo.
    _plan_for_mongo = await persist_plan(
        session_id=session_id,
        provider=provider,
        service_slug=service_slug,
        plan=plan.model_dump(),
    )

    # 1. Persist to MongoDB
    update_fields: dict[str, Any] = {
        "plan": _plan_for_mongo,
        "status": SessionStatus.REVIEWING.value,
        "updated_at": __import__("datetime").datetime.utcnow(),
        "package_structure": package_structure,
    }
    if recommended_features:
        update_fields["recommended_features"] = recommended_features
    if default_config_fields:
        update_fields["default_config_fields"] = default_config_fields

    await sessions_collection().update_one({"_id": oid}, {"$set": update_fields})

    # 2. Persist to R2 / local dir cache (source of truth — same as generate_plan_stream)
    _, guidelines_version = await _get_guidelines_for_planning()
    try:
        await r2_service.save_prompt_and_plan(
            provider,
            service_slug,
            tenant_id,
            user_prompt,
            {
                "steps": plan.model_dump()["steps"],
                "version": plan_data.get("version", 1),
                "package_structure": package_structure,
                "recommended_features": recommended_features,
                "default_config_fields": default_config_fields,
            },
            guidelines_version=guidelines_version,
        )
        logger.info(
            "plan.imported_r2_saved",
            session_id=session_id,
            provider=provider,
            service_slug=service_slug,
            tenant_id=tenant_id,
        )
    except Exception as r2_exc:
        logger.warning("plan.imported_r2_save_failed", session_id=session_id, error=str(r2_exc))

    logger.info("plan.imported_from_local_claude", session_id=session_id, step_count=len(steps))
    return {
        **plan.model_dump(),
        "package_structure": package_structure,
        "recommended_features": recommended_features,
        "default_config_fields": default_config_fields,
    }
