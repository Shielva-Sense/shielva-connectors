"""Integration Builder — AI Code Analysis Service.

Uses Gemini to analyse connector.py and produce:
  - code_sections: logical blocks with heading, plain-English description, and code
  - sequence_diagram: Mermaid sequenceDiagram showing connector interaction flow

Results are stored in R2 at CONNECTOR_DOCS/{tenant}/{provider}/{slug}/code_analysis.json
and returned to the frontend for rendering in the Code Explorer.
"""

import json
import re
from pathlib import Path
from typing import Any

import structlog
from bson import ObjectId

from integration.core.config import settings
from integration.db.database import sessions_collection
from integration.services import r2_service

logger = structlog.get_logger(__name__)

_R2_KEY_SUFFIX = "code_analysis.json"

# Warn when connector.py exceeds this size — Gemini may truncate silently
_SOURCE_WARN_CHARS = 20_000
# Valid section type values
_VALID_SECTION_TYPES = {
    "imports",
    "config",
    "auth",
    "install",
    "sync",
    "helper",
    "error-handling",
    "class",
}


def _analysis_r2_key(tenant_id: str, provider: str, service_slug: str) -> str:
    return f"CONNECTOR_DOCS/{tenant_id}/{provider}/{service_slug}/{_R2_KEY_SUFFIX}"


def _output_dir(tenant_id: str, service_slug: str) -> Path:
    import re as _re

    base = Path(settings.GENERATED_CODE_DIR).resolve()
    clean = _re.sub(r"_connector$", "", service_slug) if service_slug.endswith("_connector") else service_slug
    return base / tenant_id / f"{clean}_connector"


_ANALYSIS_SYSTEM = """You are a senior software engineer explaining a Python connector to a non-technical audience.

Analyse the connector.py file and return a JSON object with EXACTLY two keys: "sections" and "sequence_diagram".

## "sections" — array of logical code blocks (REQUIRED: 6-12 sections)
Each element MUST have ALL five fields:
{
  "id": "kebab-case-unique-id",
  "heading": "Short, clear title (e.g. 'Gmail Authentication', 'Email Sync Logic')",
  "description": "2-4 sentences in plain English explaining what this block does and WHY. No jargon. Imagine explaining to a product manager.",
  "code": "the exact Python code for this section (non-empty string)",
  "type": "one of: imports | config | auth | install | sync | helper | error-handling | class"
}
Rules:
- Produce 6-12 sections. NEVER fewer than 6. Group related helpers together (don't split by function).
- Start with imports/config, end with the class definition or a summary block.
- Every section MUST have all five fields — missing any field is invalid.
- "type" MUST be exactly one of: imports, config, auth, install, sync, helper, error-handling, class.
- "id" MUST be kebab-case (lowercase, hyphens only, no spaces/underscores).

## "sequence_diagram" — valid Mermaid sequenceDiagram (REQUIRED)
Rules:
- MUST start with exactly: sequenceDiagram
- MUST include participants: App, Connector, ExternalAPI (named after the real service), ShielvaKB
- Cover the full lifecycle: install → authorize (if OAuth) → sync → ingest into KB
- Use valid Mermaid syntax. Allowed arrow types: ->>, -->>. NO invalid arrow types.
- MUST be a non-empty string.

## Output format
Return ONLY the raw JSON object. No markdown fences (no ```json). No prose. No explanation.
The output must be parseable by json.loads() with no preprocessing.

Example minimal valid output:
{
  "sections": [
    {"id": "imports", "heading": "Imports", "description": "Sets up...", "code": "import ...", "type": "imports"}
  ],
  "sequence_diagram": "sequenceDiagram\\n    participant App\\n    participant Connector\\n    App->>Connector: install()"
}"""


async def _call_gemini(prompt: str, system: str) -> str:
    """Call Gemini via the shared streaming llm_client — single source of truth."""
    from integration.services.llm_client import _call_gemini as _shared_gemini

    return await _shared_gemini(
        [{"role": "user", "content": prompt}],
        system=system,
        max_tokens=32768,
    )


def _clean_json(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())
    return raw.strip()


def _validate_and_repair_sections(sections: list) -> tuple[list, list]:
    """Validate section objects and normalise minor issues.

    Returns (valid_sections, warnings).
    - Sections missing required fields are dropped (with a warning).
    - Unknown type values are normalised to 'helper' (with a warning).
    - IDs are normalised to kebab-case.
    """
    warnings = []
    valid = []
    seen_ids: set = set()

    for i, sec in enumerate(sections):
        if not isinstance(sec, dict):
            warnings.append(f"Section {i}: not a dict, skipped")
            continue

        # Check required fields
        missing = [f for f in ("id", "heading", "description", "code", "type") if not sec.get(f)]
        if missing:
            warnings.append(f"Section {i} ('{sec.get('heading', '?')}') missing fields: {missing} — skipped")
            continue

        # Normalise id to kebab-case
        sec_id = re.sub(r"[^a-z0-9-]", "-", sec["id"].lower().strip()).strip("-")
        if not sec_id:
            sec_id = f"section-{i}"
        if sec_id in seen_ids:
            sec_id = f"{sec_id}-{i}"
        seen_ids.add(sec_id)
        sec = dict(sec, id=sec_id)

        # Normalise type
        if sec["type"] not in _VALID_SECTION_TYPES:
            warnings.append(f"Section '{sec_id}': unknown type '{sec['type']}' → normalised to 'helper'")
            sec = dict(sec, type="helper")

        valid.append(sec)

    return valid, warnings


def _validate_mermaid(diagram: str) -> tuple[bool, str]:
    """Basic Mermaid sequenceDiagram syntax validation.

    Returns (is_valid, error_message).
    Checks:
    - Starts with 'sequenceDiagram'
    - Contains at least one participant or actor
    - Contains at least one arrow (->> or -->>)
    - No obviously invalid tokens
    """
    stripped = diagram.strip()
    if not stripped.startswith("sequenceDiagram"):
        return False, "Sequence diagram must start with 'sequenceDiagram'"
    if "->>" not in stripped and "-->>" not in stripped:
        return False, "Sequence diagram has no arrows (->>, ->>)"
    if not re.search(r"\b(participant|actor)\s+\w+", stripped):
        return False, "Sequence diagram has no participant/actor declarations"
    return True, ""


def _repair_mermaid(diagram: str, connector_name: str) -> str:
    """Attempt to fix common Mermaid issues:
    - Add sequenceDiagram prefix if missing
    - Remove invalid arrow types
    """
    stripped = diagram.strip()
    if not stripped.startswith("sequenceDiagram"):
        stripped = f"sequenceDiagram\n    {stripped}"
    # Replace ->> that are not part of valid arrow
    return re.sub(r"(?<!-)-(?!>)>(?!>)", "->>", stripped)


async def delete_code_analysis(session_id: str, tenant_id: str) -> bool:
    """Delete stored code analysis from R2 for this session's connector."""
    try:
        oid = ObjectId(session_id)
    except Exception:
        return False

    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"provider": 1, "service_slug": 1},
    )
    if not session:
        return False

    provider = session.get("provider", "")
    service_slug = session.get("service_slug", "")
    if not provider or not service_slug:
        return False

    return await r2_service.delete_connector_docs(tenant_id, provider, f"{service_slug}__code_analysis")


async def get_code_analysis(session_id: str, tenant_id: str) -> dict | None:
    """Load stored code analysis from R2, or return None if not generated yet."""
    try:
        oid = ObjectId(session_id)
    except Exception:
        return None

    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"provider": 1, "service_slug": 1},
    )
    if not session:
        return None

    provider = session.get("provider", "")
    service_slug = session.get("service_slug", "")
    if not provider or not service_slug:
        return None

    return await r2_service.get_connector_docs(tenant_id, provider, f"{service_slug}__code_analysis")


async def generate_code_analysis(session_id: str, tenant_id: str) -> dict:
    """Generate AI code analysis for the connector and persist to R2."""
    try:
        oid = ObjectId(session_id)
    except Exception:
        raise ValueError(f"Invalid session ID: {session_id}")

    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": tenant_id},
        {"provider": 1, "service_slug": 1, "service_name": 1, "connector_name": 1},
    )
    if not session:
        raise ValueError(f"Session not found: {session_id}")

    provider = session.get("provider", "")
    service_slug = session.get("service_slug", "")
    service_name = session.get("service_name", service_slug)
    connector_name = session.get("connector_name", service_name)

    out_dir = _output_dir(tenant_id, service_slug)
    connector_path = out_dir / "connector.py"

    if not connector_path.exists():
        raise ValueError("connector.py not found — run execution first")

    connector_source = connector_path.read_text(encoding="utf-8")

    # Warn early if source is large — Gemini may produce incomplete analysis
    source_len = len(connector_source)
    truncation_warning = None
    if source_len > _SOURCE_WARN_CHARS:
        truncation_warning = (
            f"connector.py is {source_len:,} chars — Gemini may not analyse all sections. "
            f"Consider splitting the connector into smaller modules."
        )
        logger.warning("code_analysis.large_source", chars=source_len, session_id=session_id)

    prompt = f"Analyse this connector for {connector_name} ({provider}).\n\n```python\n{connector_source}\n```"

    # ── Retry loop: up to 2 attempts with parse-error correction ──
    result: dict[str, Any] = {}
    last_error = ""
    for attempt in range(2):
        if attempt > 0:
            # Inject parse error into prompt so Gemini self-corrects
            prompt = (
                f"{prompt}\n\n"
                f"Your previous response failed to parse: {last_error}\n"
                f"Return ONLY a raw JSON object with 'sections' and 'sequence_diagram'. "
                f"No markdown fences. No prose. Start with {{ and end with }}."
            )

        _system = await r2_service.get_step_prompt("ANALYSIS_SYSTEM", _ANALYSIS_SYSTEM)
        raw = await _call_gemini(prompt, _system)
        cleaned = _clean_json(raw)

        try:
            result = json.loads(cleaned)
            break  # success
        except json.JSONDecodeError as e:
            last_error = f"JSONDecodeError at position {e.pos}: {e.msg} (preview: {cleaned[:200]})"
            logger.warning(
                "code_analysis.json_parse_failed",
                attempt=attempt,
                error=last_error,
                session_id=session_id,
            )
            if attempt == 1:
                raise ValueError(f"Gemini returned invalid JSON after {attempt + 1} attempts: {e}")

    # ── Validate required top-level fields ──
    if not result.get("sections"):
        raise ValueError("Analysis missing 'sections' field")
    if not isinstance(result["sections"], list):
        raise ValueError("'sections' must be a list")
    if not result.get("sequence_diagram"):
        raise ValueError("Analysis missing 'sequence_diagram' field")

    # ── Validate and repair sections ──
    valid_sections, section_warnings = _validate_and_repair_sections(result["sections"])
    if not valid_sections:
        raise ValueError(f"All sections failed validation. Warnings: {section_warnings}")
    if len(valid_sections) < 3:
        logger.warning(
            "code_analysis.too_few_sections",
            count=len(valid_sections),
            dropped=len(result["sections"]) - len(valid_sections),
        )
    if section_warnings:
        logger.warning("code_analysis.section_warnings", warnings=section_warnings)
    result["sections"] = valid_sections

    # ── Validate Mermaid diagram ──
    diagram = result["sequence_diagram"].strip()
    mermaid_ok, mermaid_err = _validate_mermaid(diagram)
    if not mermaid_ok:
        logger.warning("code_analysis.mermaid_invalid", error=mermaid_err, session_id=session_id)
        # Attempt repair before giving up
        repaired = _repair_mermaid(diagram, connector_name)
        mermaid_ok2, _ = _validate_mermaid(repaired)
        if mermaid_ok2:
            result["sequence_diagram"] = repaired
            logger.info("code_analysis.mermaid_repaired", session_id=session_id)
        else:
            # Store the original but flag for frontend display
            result["sequence_diagram_error"] = mermaid_err

    # ── Attach metadata ──
    result["connector_name"] = connector_name
    result["provider"] = provider
    result["generated_at"] = __import__("datetime").datetime.utcnow().isoformat()
    result["section_count"] = len(valid_sections)
    if section_warnings:
        result["section_warnings"] = section_warnings
    if truncation_warning:
        result["truncation_warning"] = truncation_warning

    # Store in R2 using a composite slug so it doesn't collide with docs.json
    await r2_service.save_connector_docs(
        tenant_id=tenant_id,
        provider=provider,
        service_slug=f"{service_slug}__code_analysis",
        docs=result,
    )

    logger.info(
        "code_analysis.generated",
        session_id=session_id,
        sections=len(valid_sections),
        mermaid_ok=mermaid_ok,
        source_chars=source_len,
    )
    return result
