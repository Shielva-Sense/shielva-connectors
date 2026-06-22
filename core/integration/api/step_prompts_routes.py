"""Integration Builder — Step Prompts API routes.

Exposes CRUD for the versioned LLM step prompts stored in R2 (STEP_PROMPTS/ prefix).
Prompts can be read and updated at runtime without a code deployment.
"""

import structlog
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from typing import Dict, Optional

from integration.services import r2_service
from integration.prompts import codegen_prompt

logger = structlog.get_logger(__name__)

step_prompts_router = APIRouter(prefix="/step-prompts", tags=["step-prompts"])

# Canonical list of prompt names managed by R2
MANAGED_PROMPTS = [
    # Tier-1: codegen prompts
    "CONNECTOR_SYSTEM_PROMPT",
    "TEST_SYSTEM_PROMPT",
    "INTEGRATION_TEST_SYSTEM_PROMPT",
    "FIX_CODE_PROMPT",
    "FIX_TESTS_PROMPT",
    "FIX_CONNECTOR_FOR_TESTS_PROMPT",
    "TEST_RULES_GENERATION_PROMPT",
    "MODULE_FILE_SYSTEM_PROMPT",
    "TEST_MODULE_SYSTEM_PROMPT",
    "USER_MODIFY_PROMPT",
    "USER_RESTRUCTURE_PROMPT",
    # Tier-2: agentic/step prompts
    "CONNECTOR_GEN_SYSTEM",
    "CONNECTOR_GEN_SYSTEM_oauth2_code",
    "CONNECTOR_GEN_SYSTEM_oauth2_pkce",
    "CONNECTOR_GEN_SYSTEM_oauth2_client_credentials",
    "CONNECTOR_GEN_SYSTEM_api_key",
    "CONNECTOR_GEN_SYSTEM_service_account",
    "CONNECTOR_GEN_SYSTEM_basic_auth",
    "METADATA_GEN_SYSTEM",
    "DOCS_GEN_SYSTEM",
    "FIX_SYSTEM",
    "METADATA_SYSTEM_PROMPT",
    "SETUP_INSTRUCTIONS_SYSTEM",
    "TEST_GUIDELINES_SYSTEM",
    # Tier-3: previously hardcoded
    "PLANNING_SYSTEM_PROMPT",
    "REPLAN_SYSTEM_PROMPT",
    "DOCS_GENERATION_PROMPT",
    "DOCS_UPDATE_PROMPT",
    "SUGGEST_SERVICES_SYSTEM",
    "SUGGEST_DEPS_SYSTEM",
    "ANALYSIS_SYSTEM",
    "SYNTHESIS_PROMPT",
    "EXTRACTION_PROMPT",
    "TEST_GEN_SYSTEM",
    "CONNECTOR_FIX_SYSTEM",
]

# Map name → local fallback constant (Tier-1 only; rest handled by _get_fallback)
_LOCAL_FALLBACKS: Dict[str, str] = {
    "CONNECTOR_SYSTEM_PROMPT": codegen_prompt.CONNECTOR_SYSTEM_PROMPT,
    "TEST_SYSTEM_PROMPT": codegen_prompt.TEST_SYSTEM_PROMPT,
    "INTEGRATION_TEST_SYSTEM_PROMPT": codegen_prompt.INTEGRATION_TEST_SYSTEM_PROMPT,
    "FIX_CODE_PROMPT": codegen_prompt.FIX_CODE_PROMPT,
    "FIX_TESTS_PROMPT": codegen_prompt.FIX_TESTS_PROMPT,
    "FIX_CONNECTOR_FOR_TESTS_PROMPT": codegen_prompt.FIX_CONNECTOR_FOR_TESTS_PROMPT,
    "TEST_RULES_GENERATION_PROMPT": codegen_prompt.TEST_RULES_GENERATION_PROMPT,
    "MODULE_FILE_SYSTEM_PROMPT": codegen_prompt.MODULE_FILE_SYSTEM_PROMPT,
    "TEST_MODULE_SYSTEM_PROMPT": codegen_prompt.TEST_MODULE_SYSTEM_PROMPT,
    "USER_MODIFY_PROMPT": codegen_prompt.USER_MODIFY_PROMPT,
    "USER_RESTRUCTURE_PROMPT": codegen_prompt.USER_RESTRUCTURE_PROMPT,
}


def _get_fallback(prompt_name: str) -> str:
    """Return hardcoded fallback for any managed prompt. Uses lazy imports to avoid circular issues."""
    if prompt_name in _LOCAL_FALLBACKS:
        return _LOCAL_FALLBACKS[prompt_name]
    try:
        # Tier-2: agentic/step prompts
        if prompt_name in ("CONNECTOR_GEN_SYSTEM", "METADATA_GEN_SYSTEM", "DOCS_GEN_SYSTEM",
                           "FIX_SYSTEM", "CONNECTOR_FIX_SYSTEM", "TEST_GEN_SYSTEM") or \
           prompt_name.startswith("CONNECTOR_GEN_SYSTEM_"):
            from integration.services.agentic_fix import (
                _CONNECTOR_GEN_SYSTEM, _METADATA_GEN_SYSTEM, _DOCS_GEN_SYSTEM,
                _FIX_SYSTEM, _CONNECTOR_FIX_SYSTEM, _TEST_GEN_SYSTEM, _AUTH_TYPE_ADDENDA,
            )
            _agentic = {
                "CONNECTOR_GEN_SYSTEM": _CONNECTOR_GEN_SYSTEM,
                "METADATA_GEN_SYSTEM": _METADATA_GEN_SYSTEM,
                "DOCS_GEN_SYSTEM": _DOCS_GEN_SYSTEM,
                "FIX_SYSTEM": _FIX_SYSTEM,
                "CONNECTOR_FIX_SYSTEM": _CONNECTOR_FIX_SYSTEM,
                "TEST_GEN_SYSTEM": _TEST_GEN_SYSTEM,
                **{f"CONNECTOR_GEN_SYSTEM_{k}": v for k, v in _AUTH_TYPE_ADDENDA.items()},
            }
            return _agentic.get(prompt_name, "")
        if prompt_name in ("METADATA_SYSTEM_PROMPT", "SETUP_INSTRUCTIONS_SYSTEM", "TEST_GUIDELINES_SYSTEM"):
            from integration.services.step_executor import (
                _METADATA_SYSTEM_PROMPT, _SETUP_INSTRUCTIONS_SYSTEM, _TEST_GUIDELINES_SYSTEM,
            )
            return {"METADATA_SYSTEM_PROMPT": _METADATA_SYSTEM_PROMPT,
                    "SETUP_INSTRUCTIONS_SYSTEM": _SETUP_INSTRUCTIONS_SYSTEM,
                    "TEST_GUIDELINES_SYSTEM": _TEST_GUIDELINES_SYSTEM}.get(prompt_name, "")
        # Tier-3: planning
        if prompt_name in ("PLANNING_SYSTEM_PROMPT", "REPLAN_SYSTEM_PROMPT"):
            from integration.prompts.planning_prompt import PLANNING_SYSTEM_PROMPT, REPLAN_SYSTEM_PROMPT
            return {"PLANNING_SYSTEM_PROMPT": PLANNING_SYSTEM_PROMPT,
                    "REPLAN_SYSTEM_PROMPT": REPLAN_SYSTEM_PROMPT}.get(prompt_name, "")
        # Tier-3: docs
        if prompt_name in ("DOCS_GENERATION_PROMPT", "DOCS_UPDATE_PROMPT"):
            from integration.prompts.docs_prompt import DOCS_GENERATION_PROMPT, DOCS_UPDATE_PROMPT
            return {"DOCS_GENERATION_PROMPT": DOCS_GENERATION_PROMPT,
                    "DOCS_UPDATE_PROMPT": DOCS_UPDATE_PROMPT}.get(prompt_name, "")
        # Tier-3: catalog
        if prompt_name in ("SUGGEST_SERVICES_SYSTEM", "SUGGEST_DEPS_SYSTEM"):
            from integration.api.catalog_routes import _SUGGEST_SERVICES_SYSTEM, _SUGGEST_DEPS_SYSTEM
            return {"SUGGEST_SERVICES_SYSTEM": _SUGGEST_SERVICES_SYSTEM,
                    "SUGGEST_DEPS_SYSTEM": _SUGGEST_DEPS_SYSTEM}.get(prompt_name, "")
        # Tier-3: analysis / synthesis
        if prompt_name == "ANALYSIS_SYSTEM":
            from integration.services.code_analysis_service import _ANALYSIS_SYSTEM
            return _ANALYSIS_SYSTEM
        if prompt_name in ("SYNTHESIS_PROMPT", "EXTRACTION_PROMPT"):
            from integration.services.docs_synth_service import _SYNTHESIS_PROMPT, _EXTRACTION_PROMPT
            return {"SYNTHESIS_PROMPT": _SYNTHESIS_PROMPT,
                    "EXTRACTION_PROMPT": _EXTRACTION_PROMPT}.get(prompt_name, "")
    except Exception as _e:
        logger.warning("step_prompt.fallback_load_failed", prompt=prompt_name, error=str(_e))
    return ""


class UpdatePromptRequest(BaseModel):
    content: str


class StepPromptResponse(BaseModel):
    name: str
    content: str
    source: str  # "r2" | "local_cache" | "local_fallback"


# ── Routes ────────────────────────────────────────────────────────────

@step_prompts_router.get("", response_model=Dict[str, str])
async def list_step_prompts():
    """List all managed step prompt names."""
    return {"prompts": MANAGED_PROMPTS}


@step_prompts_router.get("/{prompt_name}")
async def get_step_prompt(prompt_name: str):
    """Get the active content of a step prompt (R2 → local fallback)."""
    if prompt_name not in MANAGED_PROMPTS:
        raise HTTPException(status_code=404, detail=f"Unknown prompt: {prompt_name}. Valid names: {MANAGED_PROMPTS}")

    fallback = _get_fallback(prompt_name)
    content = await r2_service.get_step_prompt(prompt_name, fallback)
    source = "r2_or_local_cache" if content != fallback else "local_fallback"
    return {"name": prompt_name, "content": content, "source": source, "length": len(content)}


@step_prompts_router.put("/{prompt_name}")
async def update_step_prompt(
    prompt_name: str,
    body: UpdatePromptRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Update a step prompt in R2. Takes effect immediately (in-process cache cleared).

    Used to fix contradictions or improve prompts without redeploying code.
    """
    if prompt_name not in MANAGED_PROMPTS:
        raise HTTPException(status_code=404, detail=f"Unknown prompt: {prompt_name}. Valid names: {MANAGED_PROMPTS}")

    if len(body.content.strip()) < 50:
        raise HTTPException(status_code=400, detail="Prompt content too short — likely an accident")

    try:
        await r2_service.save_step_prompt(prompt_name, body.content)
        logger.info("step_prompt.updated_via_api", prompt=prompt_name, tenant_id=x_tenant_id, chars=len(body.content))
        return {"name": prompt_name, "status": "saved", "chars": len(body.content)}
    except Exception as exc:
        logger.error("step_prompt.update_failed", prompt=prompt_name, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@step_prompts_router.post("/sync")
async def sync_prompts_to_r2(x_tenant_id: str = Header(..., alias="X-Tenant-ID")):
    """Re-sync all local hardcoded prompts to R2 (skips prompts already present in R2).

    Use this after a code deployment that updated codegen_prompt.py, to push
    the changes to R2 so they override the previous version.
    Call with force=true query param to overwrite even existing R2 prompts.
    """
    results = await r2_service.sync_all_step_prompts_to_r2()
    return {"results": results, "total": len(results)}


@step_prompts_router.get("/v2/builder-advanced")
async def get_builder_advanced_prompts(auth_type: str = "api_key"):
    """Batch endpoint for Builder Advanced CLI mode.

    Returns all managed prompts needed by the advanced builder in one call.
    Fetches from R2 (falling back to local) for each prompt.
    Includes:
    - CONNECTOR_GEN_SYSTEM (+ auth-type addendum merged in)
    - CODE_EXECUTION_GUIDELINES (OCP/SRP/package structure rules)
    - TEST_GEN_SYSTEM, FIX_TESTS_PROMPT, CONNECTOR_FIX_SYSTEM, METADATA_GEN_SYSTEM
    """
    from integration.services.guidelines_service import get_active_guidelines

    prompt_names = [
        "CONNECTOR_GEN_SYSTEM",
        "TEST_GEN_SYSTEM",
        "FIX_TESTS_PROMPT",
        "CONNECTOR_FIX_SYSTEM",
        "METADATA_GEN_SYSTEM",
        "PLANNING_SYSTEM_PROMPT",
        "TEST_GUIDELINES_SYSTEM",
    ]

    results: dict[str, str] = {}
    for name in prompt_names:
        fallback = _get_fallback(name)
        results[name] = await r2_service.get_step_prompt(name, fallback)

    # Merge auth-type addendum on top of CONNECTOR_GEN_SYSTEM if available
    auth_variant_name = f"CONNECTOR_GEN_SYSTEM_{auth_type}"
    if auth_variant_name in MANAGED_PROMPTS:
        addendum_fallback = _get_fallback(auth_variant_name)
        addendum = await r2_service.get_step_prompt(auth_variant_name, addendum_fallback)
        if addendum.strip():
            results["CONNECTOR_GEN_SYSTEM"] = results["CONNECTOR_GEN_SYSTEM"] + "\n\n" + addendum

    # Fetch CODE_EXECUTION_GUIDELINES (OCP/SRP/package structure/import rules)
    # These are injected into CONNECTOR_GEN_SYSTEM so the CLI prompt has all rules
    try:
        guidelines_record = await get_active_guidelines()
        guidelines_content = guidelines_record.get("content", "")
        if guidelines_content:
            results["CODE_EXECUTION_GUIDELINES"] = guidelines_content
            # Also prepend into CONNECTOR_GEN_SYSTEM so Claude CLI sees the full ruleset
            results["CONNECTOR_GEN_SYSTEM"] = (
                f"# Implementation Guidelines\n{guidelines_content}\n\n"
                + results["CONNECTOR_GEN_SYSTEM"]
            )
    except Exception:
        results["CODE_EXECUTION_GUIDELINES"] = ""

    return {"prompts": results, "auth_type": auth_type}


@step_prompts_router.post("/sync/force")
async def force_sync_prompts_to_r2(x_tenant_id: str = Header(..., alias="X-Tenant-ID")):
    """Force-overwrite ALL R2 step prompts with the current local hardcoded versions.

    Use this when you've fixed contradictions in codegen_prompt.py and want
    to push those fixes to R2, overriding any manual edits.
    """
    # Invalidate entire in-process cache so sync_all picks up fresh values
    r2_service._step_prompt_cache.clear()
    results = await r2_service.sync_all_step_prompts_to_r2()
    logger.info("step_prompt.force_sync_complete", tenant_id=x_tenant_id, total=len(results))
    return {"results": results, "total": len(results)}
