"""Integration Builder — Documentation Builder service.

Generates structured connector documentation as JSON that the frontend
renders via the SiteRenderer component. Uses the LLM to produce docs
from the generated connector source code and metadata.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import structlog
from bson import ObjectId

from integration.core.config import settings
from integration.db.database import sessions_collection
from integration.prompts.docs_prompt import DOCS_GENERATION_PROMPT, DOCS_UPDATE_PROMPT
from integration.services.docs_guidelines_service import get_active_doc_guidelines
from integration.services import knowledge_service, r2_service
from integration.services.llm_client import call_llm

logger = structlog.get_logger(__name__)

# Max retry attempts for JSON parsing
_MAX_JSON_RETRIES = 2


def _output_dir(tenant_id: str, service_slug: str) -> Path:
    """Return the directory where generated code is written.

    Mirrors step_executor._output_dir:
    Path: {GENERATED_CODE_DIR}/{tenant_id}/{service_slug}_connector/
    """
    import re as _re
    base = Path(settings.GENERATED_CODE_DIR).resolve()
    clean = _re.sub(r'_connector$', '', service_slug) if service_slug.endswith('_connector') else service_slug
    return base / tenant_id / f"{clean}_connector"


def _read_file_safe(path: Path) -> str:
    """Read a file and return its content, or a placeholder if missing."""
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("docs_builder.read_file_error", path=str(path), error=str(exc))
    return f"# File not found: {path.name}"


def _normalise_to_sections(flat: dict, connector_name: str) -> dict:
    """Convert a flat docs structure (overview/auth/methods/...) to SiteRenderer sections format.

    Called as a fallback if Gemini returns the wrong schema despite prompt instructions.
    """
    import textwrap

    def _md(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(f"- {item}" for item in content)
        if isinstance(content, dict):
            return "\n".join(f"**{k}**: {v}" for k, v in content.items())
        return str(content)

    sections = []

    if flat.get("overview"):
        sections.append({"id": "overview", "title": "Overview", "content": _md(flat["overview"])})

    if flat.get("auth"):
        auth = flat["auth"]
        content = f"**Type**: {auth.get('type', 'unknown')}\n\n{auth.get('description', '')}"
        if auth.get("fields"):
            content += "\n\n| Field | Description |\n|---|---|\n"
            for f in auth["fields"]:
                content += f"| `{f.get('name','')}` | {f.get('description','')} |\n"
        sections.append({"id": "authentication", "title": "Authentication", "content": content})

    if flat.get("methods"):
        children = []
        for m in flat["methods"]:
            params_md = ""
            if m.get("params"):
                params_md = "\n\n**Parameters:**\n| Param | Type | Required | Description |\n|---|---|---|---|\n"
                for p in m["params"]:
                    params_md += f"| `{p.get('name','')}` | {p.get('type','')} | {p.get('required','')} | {p.get('description','')} |\n"
            content = m.get("description", "") + params_md
            if m.get("returns"):
                content += f"\n\n**Returns**: {m['returns']}"
            if m.get("example"):
                content += f"\n\n**Example**:\n```python\n{m['example']}\n```"
            children.append({"id": f"method-{m.get('name','').replace('_','-')}", "title": m.get("name", ""), "content": content})
        sections.append({"id": "api-reference", "title": "API Reference", "content": "Available methods:", "children": children})

    if flat.get("setup_guide"):
        steps = flat["setup_guide"]
        content = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps)) if isinstance(steps, list) else _md(steps)
        sections.append({"id": "setup-guide", "title": "Setup Guide", "content": content})

    if flat.get("troubleshooting"):
        items = flat["troubleshooting"]
        if isinstance(items, list) and items and isinstance(items[0], dict):
            content = "| Problem | Solution |\n|---|---|\n"
            for item in items:
                content += f"| {item.get('problem','')} | {item.get('solution','')} |\n"
        else:
            content = _md(items)
        sections.append({"id": "troubleshooting", "title": "Troubleshooting", "content": content})

    if not sections:
        # Last resort: dump everything as a single overview section
        sections.append({"id": "overview", "title": "Overview", "content": json.dumps(flat, indent=2)})

    return {"title": flat.get("title", f"{connector_name} Documentation"), "sections": sections}


def _strip_json_fences(raw: str) -> str:
    """Strip markdown code fences from LLM output to extract raw JSON."""
    text = raw.strip()
    # Remove ```json ... ``` or ``` ... ```
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _extract_json_object(raw: str) -> str:
    """Extract the first JSON object from a string.

    Handles cases where the LLM wraps the JSON in extra text or fences.
    """
    text = _strip_json_fences(raw)

    # Try to find the first { and last } for the JSON object
    first_brace = text.find("{")
    if first_brace == -1:
        return text

    # Find the matching closing brace by counting depth
    depth = 0
    for i, ch in enumerate(text[first_brace:], start=first_brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[first_brace : i + 1]

    # Fallback: return from first brace to end
    return text[first_brace:]


def _validate_sections(sections: list) -> tuple:
    """Validate and repair docs sections.

    Returns (valid_sections, warnings).
    - Sections missing required fields (id, title, content) are dropped with a warning.
    - IDs are normalised to kebab-case.
    - Children arrays are recursively validated.
    """
    import re as _re
    warnings = []
    valid = []
    seen_ids: set = set()

    for i, sec in enumerate(sections):
        if not isinstance(sec, dict):
            warnings.append(f"Section {i}: not a dict, skipped")
            continue

        missing = [f for f in ("id", "title", "content") if not str(sec.get(f, "")).strip()]
        if missing:
            warnings.append(f"Section {i} ('{sec.get('title', '?')}') missing: {missing} — skipped")
            continue

        # Normalise id
        sec_id = _re.sub(r"[^a-z0-9-]", "-", sec["id"].lower().strip()).strip("-") or f"section-{i}"
        if sec_id in seen_ids:
            sec_id = f"{sec_id}-{i}"
        seen_ids.add(sec_id)
        sec = dict(sec, id=sec_id)

        # Recursively validate children
        if sec.get("children") and isinstance(sec["children"], list):
            valid_children, child_warnings = _validate_sections(sec["children"])
            sec = dict(sec, children=valid_children)
            warnings.extend(child_warnings)

        valid.append(sec)

    return valid, warnings


def _check_section_coverage(sections: list, guidelines_text: str) -> list:
    """Compare generated sections against guideline headings.

    Returns list of warning strings for sections mentioned in guidelines
    but absent from the generated docs.
    """
    if not guidelines_text:
        return []

    # Extract expected headings from guidelines (lines starting with ## or ###)
    import re as _re
    expected_headings = [
        m.group(1).strip().lower()
        for m in _re.finditer(r"^#{2,3}\s+(.+)$", guidelines_text, _re.MULTILINE)
    ]
    if not expected_headings:
        return []

    # Flatten all section titles
    def _all_titles(secs):
        for s in secs:
            yield s.get("title", "").lower()
            yield from _all_titles(s.get("children") or [])

    generated_titles = set(_all_titles(sections))

    missing = []
    for heading in expected_headings:
        # Fuzzy match: check if any generated title contains or is contained by this heading
        if not any(heading in t or t in heading for t in generated_titles):
            missing.append(heading)

    return [f"Expected section '{h}' from guidelines not found in output" for h in missing[:5]]


def _parse_docs_json(raw: str) -> dict:
    """Parse the LLM response as a docs JSON object.

    Strips code fences, extracts JSON, validates structure and per-section fields.
    Raises ValueError on failure.
    """
    extracted = _extract_json_object(raw)
    try:
        data = json.loads(extracted)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM response is not valid JSON: {exc}\n"
            f"Raw preview: {raw[:500]}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object, got {type(data).__name__}")
    if "sections" not in data:
        raise ValueError("Missing required 'sections' field in docs JSON")
    if not isinstance(data["sections"], list):
        raise ValueError("'sections' must be a list")

    # Per-section validation and repair
    valid_sections, warnings = _validate_sections(data["sections"])
    if warnings:
        logger.warning("docs_builder.section_warnings", count=len(warnings), warnings=warnings[:5])
    if not valid_sections:
        raise ValueError(f"All sections failed validation. Issues: {warnings[:3]}")

    data["sections"] = valid_sections
    if warnings:
        data["_section_warnings"] = warnings  # carried through to logs; stripped before response

    return data


async def generate_docs(
    session_id: str,
    tenant_id: str,
    extra_prompt: str = "",
    log_cb=None,          # optional async callable(level: str, message: str)
) -> dict:
    """Generate connector documentation as SiteRenderer JSON.

    1. Load session from MongoDB to get service_slug
    2. Read all generated files from the output directory
    3. Load documentation guidelines template
    4. Call LLM with DOCS_GENERATION_PROMPT
    5. Parse the JSON response
    6. Save docs JSON to session in MongoDB
    7. Return the JSON
    """
    logger.info("docs_builder.generate_start", session_id=session_id, tenant_id=tenant_id)

    async def _log(level: str, msg: str):
        if log_cb:
            try:
                await log_cb(level, msg)
            except Exception:
                pass

    await _log("info", "📄 Starting documentation generation...")

    # 1. Load session
    try:
        oid = ObjectId(session_id)
    except Exception:
        raise ValueError(f"Invalid session ID: {session_id}")

    session = await sessions_collection().find_one({"_id": oid, "tenant_id": tenant_id})
    if not session:
        raise ValueError(f"Session not found: {session_id}")

    # Extract service info from session
    service_slug = session.get("service_slug", "")
    connector_name = session.get("connector_name") or session.get("service_name", "Unknown")

    if not service_slug:
        # Derive from service name if not stored
        service_name = session.get("service_name", "")
        service_slug = service_name.replace("-", "_").lower()

    # 2. Read generated files from the output directory
    out_dir = _output_dir(tenant_id, service_slug)
    logger.info("docs_builder.reading_files", out_dir=str(out_dir))

    connector_code = _read_file_safe(out_dir / "connector.py")
    config_code = _read_file_safe(out_dir / "config.py")
    requirements = _read_file_safe(out_dir / "requirements.txt")

    # Read ALL test files (not just test_connector.py)
    test_code_parts = []
    tests_dir = out_dir / "tests"
    if tests_dir.exists():
        for tf in sorted(tests_dir.glob("*.py")):
            tc = _read_file_safe(tf)
            if tc.strip():
                test_code_parts.append(f"# === {tf.name} ===\n{tc}")
    if not test_code_parts:
        test_code_parts.append(_read_file_safe(out_dir / "test_connector.py"))
    test_code = "\n\n".join(test_code_parts)

    # Read ALL additional Python files for complete context
    additional_files = []
    for pattern in ["__init__.py", "exceptions.py", "models.py",
                     "helpers/*.py", "client/*.py"]:
        for fp in sorted(out_dir.glob(pattern)):
            fc = _read_file_safe(fp)
            if fc.strip():
                rel = fp.relative_to(out_dir)
                additional_files.append(f"# === {rel} ===\n{fc}")
    additional_code = "\n\n".join(additional_files) if additional_files else "(no additional modules)"

    # connector.json may be in metadata/ subdirectory
    connector_json_path = out_dir / "metadata" / "connector.json"
    if not connector_json_path.exists():
        connector_json_path = out_dir / "connector.json"
    connector_json = _read_file_safe(connector_json_path)

    await _log("info", f"📂 Reading generated files from {out_dir.name}/")

    # 3. Load documentation guidelines — used for coverage auditing only, NOT injected into the LLM.
    # The LLM generates sections from the actual connector code + RAG knowledge, not from a template.
    # Generic template sections ("Release Notes", "[Describe...]") produce boilerplate; code-driven
    # generation produces connector-specific docs that are actually useful.
    guidelines_record = await get_active_doc_guidelines()
    doc_guidelines = guidelines_record.get("content", "")
    await _log("info", f"📋 Guidelines v{guidelines_record.get('version', '?')} loaded (coverage audit only — not injected into LLM)")

    # 3b. Query RAG vectors for relevant API/SDK context
    _provider = session.get("provider", "")
    _service_name = session.get("service_name", connector_name)
    _tenant_id = session.get("tenant_id", "")
    rag_context = ""
    if _tenant_id:
        try:
            rag_context = await knowledge_service.query_knowledge(
                query=f"{connector_name} {_service_name} API documentation methods authentication",
                tenant_id=_tenant_id,
                provider=_provider,
                service=_service_name,
                top_k=12,
            ) or ""
        except Exception as _e:
            logger.warning("docs_builder.rag_query_failed", error=str(_e))

    async def _knowledge_search(query: str) -> str:
        """Let Gemini query the RAG knowledge base at will during docs generation."""
        if not _tenant_id:
            return ""
        try:
            return await knowledge_service.query_knowledge(
                query=query,
                tenant_id=_tenant_id,
                provider=_provider,
                service=_service_name,
                top_k=8,
            ) or ""
        except Exception:
            return ""

    # 4. Agentic docs generation (Gemini + tool calls when TEST_LLM_MODE=gemini)
    if False:  # gemini path disabled — Claude is the only backend codegen runtime
        await _log("info", "🤖 Starting Gemini agentic docs generation...")
        from integration.services.agentic_fix import gemini_agentic_generate_docs
        agentic_result = await gemini_agentic_generate_docs(
            out_dir,
            guidelines=doc_guidelines,
            extra_prompt=extra_prompt or "",
            connector_name=connector_name,
            provider=_provider,
            service_name=_service_name,
            auth_type=session.get("auth_type", ""),
            user_prompt=session.get("user_prompt", ""),
            knowledge_fn=_knowledge_search,
            # On enhance, docs are UPDATED (missing sections added) — never rewritten.
            is_enhance=session.get("run_kind") == "enhance",
            log_cb=log_cb,
            rag_context=rag_context,
        )
        docs_output_path = out_dir / "docs" / "connector_docs.json"
        if agentic_result["success"] and docs_output_path.exists():
            try:
                docs_json = json.loads(docs_output_path.read_text(encoding="utf-8"))
                if not docs_json.get("title"):
                    docs_json["title"] = f"{connector_name} Documentation"
                # Normalise: if Gemini returned flat structure instead of sections, convert it
                if not docs_json.get("sections"):
                    docs_json = _normalise_to_sections(docs_json, connector_name)
                else:
                    # Validate per-section fields; drop malformed sections with warning
                    valid_secs, sec_warns = _validate_sections(docs_json["sections"])
                    if sec_warns:
                        logger.warning("docs_builder.section_warnings", warnings=sec_warns[:5])
                    docs_json["sections"] = valid_secs
                # Guidelines coverage check
                coverage_gaps = _check_section_coverage(docs_json.get("sections", []), doc_guidelines)
                if coverage_gaps:
                    logger.warning("docs_builder.coverage_gaps", gaps=coverage_gaps, session_id=session_id)
                    docs_json["_coverage_gaps"] = coverage_gaps
                logger.info("docs_builder.agentic_success", session_id=session_id,
                            iterations=agentic_result["iterations"],
                            sections=len(docs_json.get("sections", [])))
                # Save to session and return (skip prompt-based path)
                await sessions_collection().update_one(
                    {"_id": ObjectId(session_id)},
                    {"$set": {"docs_json": docs_json, "docs_generated_at": datetime.now(timezone.utc)}},
                )
                return docs_json
            except (json.JSONDecodeError, Exception) as _e:
                logger.warning("docs_builder.agentic_json_invalid_fallback", session_id=session_id, error=str(_e))
                # Fall through to prompt-based path

    # 4b. Build prompt and call LLM (Claude fallback)
    user_prompt_section = ""
    if extra_prompt:
        user_prompt_section = f"\n## Additional User Instructions\n{extra_prompt}\n"

    _base_gen = await r2_service.get_step_prompt("DOCS_GENERATION_PROMPT", DOCS_GENERATION_PROMPT)
    prompt_text = _base_gen.format(
        connector_name=connector_name,
        connector_code=connector_code,
        test_code=test_code,
        config_code=config_code,
        connector_json=connector_json,
        requirements=requirements,
        user_prompt=user_prompt_section,
    )

    messages = [{"role": "user", "content": prompt_text}]

    # Retry loop for JSON parsing
    last_error = None
    for attempt in range(1, _MAX_JSON_RETRIES + 1):
        try:
            raw_response = await call_llm(
                messages,
                expect_code=False,
                max_tokens=32768,
                tenant_id=tenant_id,
            )

            if not raw_response:
                raise ValueError("LLM returned an empty response")

            docs_json = _parse_docs_json(raw_response)

            # Ensure title is set
            if not docs_json.get("title"):
                docs_json["title"] = f"{connector_name} Documentation"

            logger.info(
                "docs_builder.generate_success",
                session_id=session_id,
                section_count=len(docs_json.get("sections", [])),
                attempt=attempt,
            )
            break

        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning(
                "docs_builder.json_parse_retry",
                session_id=session_id,
                attempt=attempt,
                error=str(exc),
            )
            if attempt < _MAX_JSON_RETRIES:
                # Add a correction message for the retry
                messages.append({"role": "assistant", "content": raw_response[:2000] if raw_response else ""})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your response was not valid JSON. Error: {exc}\n\n"
                        "Please output ONLY a valid JSON object with the exact schema specified. "
                        "No text before or after the JSON. No markdown code fences."
                    ),
                })
            else:
                raise ValueError(
                    f"Failed to parse docs JSON after {_MAX_JSON_RETRIES} attempts: {last_error}"
                ) from last_error

    # Guidelines coverage check (warn about missing sections, don't block)
    coverage_gaps = _check_section_coverage(docs_json.get("sections", []), doc_guidelines)
    if coverage_gaps:
        logger.warning("docs_builder.coverage_gaps", gaps=coverage_gaps, session_id=session_id)
        docs_json["_coverage_gaps"] = coverage_gaps

    # 6. Save docs JSON to session in MongoDB (tenant_id for multi-tenant isolation)
    await sessions_collection().update_one(
        {"_id": oid, "tenant_id": tenant_id},
        {
            "$set": {
                "docs_json": docs_json,
                "docs_generated_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    logger.info("docs_builder.saved_to_session", session_id=session_id)
    return docs_json


async def update_docs_with_prompt(
    session_id: str,
    tenant_id: str,
    prompt: str,
    current_json: dict,
) -> dict:
    """Update existing docs JSON based on a user prompt.

    1. Call LLM with DOCS_UPDATE_PROMPT + current JSON + user prompt
    2. Parse the JSON response
    3. Update session in MongoDB
    4. Return updated JSON
    """
    logger.info("docs_builder.update_start", session_id=session_id, tenant_id=tenant_id)

    if not current_json or not isinstance(current_json, dict) or not current_json.get("sections"):
        raise ValueError("current_json must be a non-empty dict with a 'sections' key")
    if not prompt or not prompt.strip():
        raise ValueError("prompt cannot be empty")

    try:
        oid = ObjectId(session_id)
    except Exception:
        raise ValueError(f"Invalid session ID: {session_id}")

    # Serialize current docs to string for the prompt
    current_docs_str = json.dumps(current_json, indent=2)

    _base_update = await r2_service.get_step_prompt("DOCS_UPDATE_PROMPT", DOCS_UPDATE_PROMPT)
    prompt_text = _base_update.format(
        current_docs_json=current_docs_str,
        user_prompt=prompt,
    )

    messages = [{"role": "user", "content": prompt_text}]

    # Retry loop for JSON parsing
    last_error = None
    for attempt in range(1, _MAX_JSON_RETRIES + 1):
        try:
            raw_response = await call_llm(
                messages,
                expect_code=False,
                max_tokens=32768,
                tenant_id=tenant_id,
            )

            if not raw_response:
                raise ValueError("LLM returned an empty response")

            docs_json = _parse_docs_json(raw_response)

            logger.info(
                "docs_builder.update_success",
                session_id=session_id,
                section_count=len(docs_json.get("sections", [])),
                attempt=attempt,
            )
            break

        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning(
                "docs_builder.update_json_retry",
                session_id=session_id,
                attempt=attempt,
                error=str(exc),
            )
            if attempt < _MAX_JSON_RETRIES:
                messages.append({"role": "assistant", "content": raw_response[:2000] if raw_response else ""})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your response was not valid JSON. Error: {exc}\n\n"
                        "Output ONLY the complete updated JSON object. No extra text."
                    ),
                })
            else:
                raise ValueError(
                    f"Failed to parse updated docs JSON after {_MAX_JSON_RETRIES} attempts: {last_error}"
                ) from last_error

    # Save to MongoDB
    await sessions_collection().update_one(
        {"_id": oid, "tenant_id": tenant_id},
        {
            "$set": {
                "docs_json": docs_json,
                "docs_updated_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )

    logger.info("docs_builder.update_saved", session_id=session_id)
    return docs_json


async def export_docs_html(docs_json: dict) -> str:
    """Convert docs JSON to a standalone HTML string.

    Produces a self-contained HTML document with:
    - Left sidebar with navigation links
    - Content body with rendered markdown (converted to HTML)
    - Inline CSS (no external dependencies)
    - Responsive layout

    This is a Python port of the SiteRenderer exportSiteToHtml function.
    """
    title = docs_json.get("title", "Connector Documentation")
    sections = docs_json.get("sections", [])

    # Build sidebar navigation HTML
    nav_items = []
    for section in sections:
        sid = _html_escape(section.get("id", ""))
        stitle = _html_escape(section.get("title", ""))
        nav_items.append(f'<li><a href="#{sid}">{stitle}</a>')
        children = section.get("children", [])
        if children:
            nav_items.append("<ul>")
            for child in children:
                cid = _html_escape(child.get("id", ""))
                ctitle = _html_escape(child.get("title", ""))
                nav_items.append(f'<li><a href="#{cid}">{ctitle}</a></li>')
            nav_items.append("</ul>")
        nav_items.append("</li>")

    sidebar_html = "\n".join(nav_items)

    # Build content sections HTML
    content_parts = []
    for section in sections:
        sid = _html_escape(section.get("id", ""))
        stitle = _html_escape(section.get("title", ""))
        scontent = _md_to_html(section.get("content", ""))
        content_parts.append(
            f'<section id="{sid}">'
            f"<h2>{stitle}</h2>"
            f"<div>{scontent}</div>"
        )
        for child in section.get("children", []):
            cid = _html_escape(child.get("id", ""))
            ctitle = _html_escape(child.get("title", ""))
            ccontent = _md_to_html(child.get("content", ""))
            content_parts.append(
                f'<section id="{cid}">'
                f"<h3>{ctitle}</h3>"
                f"<div>{ccontent}</div>"
                f"</section>"
            )
        content_parts.append("</section>")

    body_html = "\n".join(content_parts)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_html_escape(title)}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #1a1a2e; background: #f8f9fa; display: flex; min-height: 100vh; }}
nav {{ width: 280px; background: #1a1a2e; color: #e0e0e0; padding: 24px 16px; position: fixed; top: 0; left: 0; bottom: 0; overflow-y: auto; }}
nav h1 {{ font-size: 1.1rem; color: #14b8a6; margin-bottom: 20px; padding-bottom: 12px; border-bottom: 1px solid #2a2a4e; }}
nav ul {{ list-style: none; }}
nav ul ul {{ padding-left: 16px; }}
nav li {{ margin: 4px 0; }}
nav a {{ color: #b0b0c0; text-decoration: none; font-size: 0.9rem; display: block; padding: 6px 10px; border-radius: 6px; transition: background 0.2s, color 0.2s; }}
nav a:hover {{ background: #2a2a4e; color: #14b8a6; }}
main {{ margin-left: 280px; padding: 40px 48px; max-width: 900px; flex: 1; }}
h2 {{ font-size: 1.6rem; color: #1a1a2e; margin: 32px 0 16px; padding-bottom: 8px; border-bottom: 2px solid #14b8a6; }}
h3 {{ font-size: 1.2rem; color: #2a2a4e; margin: 24px 0 12px; }}
p {{ line-height: 1.7; margin: 8px 0; }}
table {{ width: 100%; border-collapse: collapse; margin: 12px 0; }}
th, td {{ border: 1px solid #ddd; padding: 10px 14px; text-align: left; font-size: 0.9rem; }}
th {{ background: #f0f0f5; font-weight: 600; }}
pre {{ background: #1e1e2e; color: #e0e0e0; padding: 16px; border-radius: 8px; overflow-x: auto; margin: 12px 0; font-size: 0.85rem; }}
code {{ background: #e8e8f0; padding: 2px 6px; border-radius: 4px; font-size: 0.85em; }}
pre code {{ background: none; padding: 0; }}
ul, ol {{ padding-left: 24px; margin: 8px 0; }}
li {{ margin: 4px 0; line-height: 1.6; }}
img {{ max-width: 100%; border-radius: 8px; margin: 12px 0; }}
@media (max-width: 768px) {{
  nav {{ position: static; width: 100%; }}
  main {{ margin-left: 0; padding: 20px; }}
}}
</style>
</head>
<body>
<nav>
<h1>{_html_escape(title)}</h1>
<ul>
{sidebar_html}
</ul>
</nav>
<main>
{body_html}
</main>
</body>
</html>"""


# ── Internal helpers ─────────────────────────────────────────────────

def _html_escape(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def _md_to_html(md: str) -> str:
    """Convert basic Markdown to HTML.

    Handles: headings, bold, italic, inline code, code blocks, tables,
    unordered/ordered lists, links, images, horizontal rules.
    This is intentionally lightweight — no external dependency needed.
    """
    if not md:
        return ""

    lines = md.split("\n")
    html_parts = []
    in_code_block = False
    code_block_lines = []
    in_table = False
    table_rows = []
    in_ul = False
    in_ol = False

    def _flush_list():
        nonlocal in_ul, in_ol
        if in_ul:
            html_parts.append("</ul>")
            in_ul = False
        if in_ol:
            html_parts.append("</ol>")
            in_ol = False

    def _flush_table():
        nonlocal in_table, table_rows
        if not in_table:
            return
        html_parts.append("<table>")
        for idx, row in enumerate(table_rows):
            cells = [c.strip() for c in row.split("|")]
            cells = [c for c in cells if c or idx == 0]  # keep empty cells
            # Skip separator rows (---|---|---)
            if all(re.match(r"^-+:?$|^:?-+:?$", c) for c in cells if c):
                continue
            tag = "th" if idx == 0 else "td"
            row_html = "".join(f"<{tag}>{_inline_md(c)}</{tag}>" for c in cells)
            html_parts.append(f"<tr>{row_html}</tr>")
        html_parts.append("</table>")
        in_table = False
        table_rows = []

    for line in lines:
        # Code blocks
        if line.strip().startswith("```"):
            if in_code_block:
                html_parts.append(f"<pre><code>{_html_escape(chr(10).join(code_block_lines))}</code></pre>")
                code_block_lines = []
                in_code_block = False
            else:
                _flush_list()
                _flush_table()
                in_code_block = True
            continue

        if in_code_block:
            code_block_lines.append(line)
            continue

        stripped = line.strip()

        # Horizontal rule
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            _flush_list()
            _flush_table()
            html_parts.append("<hr>")
            continue

        # Table rows
        if "|" in stripped and not stripped.startswith("#"):
            if not in_table:
                _flush_list()
                in_table = True
            # Strip leading/trailing pipe
            row = stripped.strip("|")
            table_rows.append(row)
            continue
        else:
            _flush_table()

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            _flush_list()
            level = len(heading_match.group(1))
            text = heading_match.group(2)
            html_parts.append(f"<h{level}>{_inline_md(text)}</h{level}>")
            continue

        # Unordered list
        ul_match = re.match(r"^[-*+]\s+(.+)$", stripped)
        if ul_match:
            _flush_table()
            if not in_ul:
                if in_ol:
                    html_parts.append("</ol>")
                    in_ol = False
                html_parts.append("<ul>")
                in_ul = True
            html_parts.append(f"<li>{_inline_md(ul_match.group(1))}</li>")
            continue

        # Ordered list
        ol_match = re.match(r"^\d+\.\s+(.+)$", stripped)
        if ol_match:
            _flush_table()
            if not in_ol:
                if in_ul:
                    html_parts.append("</ul>")
                    in_ul = False
                html_parts.append("<ol>")
                in_ol = True
            html_parts.append(f"<li>{_inline_md(ol_match.group(1))}</li>")
            continue

        # Close lists if we hit a non-list line
        _flush_list()

        # Empty line
        if not stripped:
            continue

        # Paragraph
        html_parts.append(f"<p>{_inline_md(stripped)}</p>")

    # Flush remaining state
    if in_code_block and code_block_lines:
        html_parts.append(f"<pre><code>{_html_escape(chr(10).join(code_block_lines))}</code></pre>")
    _flush_list()
    _flush_table()

    return "\n".join(html_parts)


def _sanitize_url(url: str) -> str:
    """Sanitize URL to prevent javascript: XSS injection."""
    url = url.strip()
    if url.lower().startswith(("javascript:", "data:", "vbscript:")):
        return "#"
    return _html_escape(url)


def _inline_md(text: str) -> str:
    """Convert inline Markdown: bold, italic, code, links, images."""
    # Images: ![alt](url) — sanitize URL to prevent XSS
    text = re.sub(
        r"!\[([^\]]*)\]\(([^)]+)\)",
        lambda m: f'<img src="{_sanitize_url(m.group(2))}" alt="{_html_escape(m.group(1))}">',
        text,
    )
    # Links: [text](url) — sanitize URL to prevent XSS
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda m: f'<a href="{_sanitize_url(m.group(2))}" target="_blank" rel="noopener noreferrer">{_html_escape(m.group(1))}</a>',
        text,
    )
    # Inline code: `code`
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
    # Italic: *text* or _text_
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", text)
    # Checkbox: - [ ] or - [x]
    text = text.replace("[ ]", "&#9744;").replace("[x]", "&#9745;").replace("[X]", "&#9745;")
    return text
