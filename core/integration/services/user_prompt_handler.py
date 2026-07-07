"""User prompt handler for WebSocket inline modifications.

Processes freeform user prompts like "fix the import error" or "add retry logic"
by identifying the target file, calling the LLM, and writing the modified code.

Supports three modes:
  - Metadata generation: detects "generate metadata / connector.json" intent and
    runs handle_generate_metadata() with full RAG + guideline context.
  - Single-file: modifies one identified file (default)
  - Multi-file restructure: detects restructuring intent and rewrites the whole
    package using helpers/, client/ separation of concerns.
"""

import ast
import contextlib
import re
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import structlog
from bson import ObjectId

from integration.prompts.codegen_prompt import (
    USER_MODIFY_PROMPT,
    USER_RESTRUCTURE_PROMPT,
)
from integration.services import r2_service
from integration.services.code_quality import analyze_file
from integration.services.llm_client import call_llm_fix, set_llm_tenant_id
from integration.services.step_executor import (
    _VALID_PYTHON_STARTS,
    _clean_llm_code_response,
    _focused_test_code,
    _output_dir,
    _sync_init_with_connector,
)

logger = structlog.get_logger(__name__)

# Type for the async event sink: (event_type, data_dict) -> None
EventSink = Callable[[str, dict[str, Any]], Coroutine[Any, Any, None]]

# ── Restructure intent detection ─────────────────────────────────────

_RESTRUCTURE_KEYWORDS = [
    # Explicit restructure intent
    "restructure",
    "reorganise",
    "reorganize",
    "reorganisation",
    "reorganization",
    "re-organise",
    "re-organize",
    "re-organisation",
    "re-organization",
    "reorganis",
    "reorganiz",  # substring covers all conjugations
    # Separation of concerns
    "separation of concerns",
    "separate concerns",
    "separations of concern",
    # Sub-package / directory names
    "helpers",
    "helper",
    "helpers/",
    "helpers dir",
    "client/",
    "client layer",
    "api client",
    "client module",
    # Refactor / split signals
    "refactor",
    "extract",
    "split into",
    "move to",
    "modular",
    "modulari",
    "separate files",
    "separate modules",
    # "utilise" patterns common in user prompts
    "utilise helpers",
    "utilise client",
    "utilize helpers",
    "utilize client",
    "use helpers",
    "use client",
]


def _is_restructure_prompt(prompt: str) -> bool:
    """Return True if the prompt intends a multi-file package restructure.

    Strips hyphens before matching so 're-organise' == 'reorganise', etc.
    Requires at least 2 keyword signals to avoid false positives.
    """
    # Normalise: lowercase + collapse hyphens so "re-organise" → "reorganise"
    pl = prompt.lower().replace("-", "")
    # Also check the raw lowercase for space/underscore variants
    pl_raw = prompt.lower()
    hits = sum(1 for kw in _RESTRUCTURE_KEYWORDS if kw.replace("-", "") in pl or kw in pl_raw)
    return hits >= 2


# ── Metadata generation intent detection ─────────────────────────────

_METADATA_KEYWORDS = [
    # Direct intent
    "generate metadata",
    "create metadata",
    "regenerate metadata",
    "update metadata",
    "rebuild metadata",
    # connector.json variants
    "connector.json",
    "generate connector.json",
    "create connector.json",
    "update connector.json",
    "rebuild connector.json",
    # guideline reference — key phrase the user will naturally type
    "as per the guideline",
    "per the guideline",
    "using the guideline",
    "per guideline",
    "follow the guideline",
    # install_fields / deploy form intent
    "install fields",
    "install_fields",
    "deploy form fields",
    # metadata + any action
    "metadata json",
    "generate the metadata",
    "write metadata",
    "create the metadata",
    "produce metadata",
]


def _is_metadata_prompt(prompt: str) -> bool:
    """Return True if the prompt is asking to generate/update connector metadata.

    Single keyword is enough — these phrases are highly specific and unlikely
    to appear in code-modification prompts.
    """
    pl = prompt.lower()
    return any(kw in pl for kw in _METADATA_KEYWORDS)


# ── File target identification (single-file mode) ────────────────────

_FILE_KEYWORDS = {
    "connector.py": [
        "connector",
        "connect",
        "sync",
        "health",
        "authorize",
        "install",
        "api call",
        "http",
        "request",
        "endpoint",
        "class name",
        "base connector",
        "retry",
        "incremental",
    ],
    "tests/test_connector.py": [
        "test",
        "tests",
        "pytest",
        "assert",
        "mock",
        "fixture",
        "test_",
        "unit test",
        "integration test",
        "import error",
        "importerror",
        "cannot import",
        "collection error",
    ],
    "config.py": [
        "config",
        "configuration",
        "setting",
        "oauth",
        "auth config",
        "credential",
        "scope",
        "client_id",
        "secret",
    ],
    "__init__.py": [
        "init",
        "__init__",
        "import name",
        "export",
        "module",
    ],
}


def _identify_target_file(prompt: str, out_dir: Path = None) -> str:
    """Identify which file the user wants to modify based on prompt content.

    Scans the ACTUAL connector directory so it works for any package structure.

    Priority order:
    1. Exact filename match in prompt against real files on disk
    2. Pytest traceback file references  (tests/foo.py:lineno:)
    3. Test error signals                (ImportError, FAILED tests/, etc.)
    4. Stem/keyword match against real files
    5. Default → connector.py
    """
    import re as _re

    prompt_lower = prompt.lower()

    # ── Build real file list from disk (works for any structure) ──
    real_files: list[str] = []
    if out_dir and out_dir.exists():
        for p in out_dir.rglob("*.py"):
            rel = str(p.relative_to(out_dir))
            if "__pycache__" not in rel:
                real_files.append(rel)

    # ── 1. Exact filename or path match against real files ──
    for f in real_files:
        fname = Path(f).name  # e.g. "test_auth.py"
        stem = Path(f).stem  # e.g. "test_auth"
        str(Path(f).parent)  # e.g. "tests"
        # Full relative path match (e.g. "tests/test_auth.py")
        if f.replace("\\", "/") in prompt_lower:
            return f
        # Just filename match (e.g. "test_auth.py")
        if fname in prompt_lower:
            return f
        # Stem match for test files (e.g. "test_auth" without .py)
        if stem in prompt_lower and stem.startswith("test_"):
            return f

    # ── 2. Pytest traceback: "tests/foo.py:7:" or "FAILED tests/foo.py::" ──
    _tb_match = _re.search(r"(?:tests|specs)/(\w[\w/]*\.py)", prompt_lower)
    if _tb_match:
        candidate = f"tests/{Path(_tb_match.group(1)).name}"
        # Verify it exists on disk
        if out_dir and (out_dir / candidate).exists():
            return candidate
        return candidate  # return it even if not found yet

    # ── 3. Test error signals → route to tests/ not connector.py ──
    _test_error_signals = [
        "importerror",
        "import error",
        "cannot import",
        "collection error",
        "error collecting",
        "no module named",
        "modulenotfounderror",
        "indentationerror",
        "syntaxerror",
        "test file",
        "test cases",
        "unit test",
        "check test",
        "fix test",
        "pytest",
        "assert",
        "mock",
        "fixture",
        "def test",
    ]
    if any(sig in prompt_lower for sig in _test_error_signals):
        # Prefer test_connector.py; fall back to first test file found
        test_files = [f for f in real_files if Path(f).stem.startswith("test_")]
        connector_test = next((f for f in test_files if "test_connector" in f), None)
        if connector_test:
            return connector_test
        if test_files:
            return test_files[0]
        return "tests/test_connector.py"

    # ── 4. Stem/keyword match against all real files ──
    for f in real_files:
        stem = Path(f).stem  # e.g. "normalizer", "http_client", "config"
        if stem in prompt_lower and stem not in ("connector", "init"):
            return f

    # ── 5. Default ──
    return "connector.py"


# ── Multi-file helpers ────────────────────────────────────────────────

_PY_EXTENSIONS = {".py", ".json", ".txt", ".md"}
_SKIP_DIRS = {"__pycache__", ".git", ".mypy_cache", "node_modules"}
_SKIP_FILES = {"*.pyc", "*.pyo"}


def _read_package_files(out_dir: Path) -> dict[str, str]:
    """Read all relevant source files from the package directory.

    Returns {relative_path: content} for Python files and stubs.
    Skips test files (too large, restructure doesn't need them).
    """
    files: dict[str, str] = {}
    for path in sorted(out_dir.rglob("*.py")):
        rel = path.relative_to(out_dir)
        parts = rel.parts
        # Skip cache dirs and test files (tests stay unchanged)
        if any(p in _SKIP_DIRS for p in parts):
            continue
        if parts[0] == "tests":
            continue
        with contextlib.suppress(Exception):
            files[str(rel)] = path.read_text(encoding="utf-8")
    return files


def _build_current_files_block(files: dict[str, str]) -> str:
    """Format the current package files into a readable block for the LLM."""
    parts = []
    for rel_path, content in files.items():
        parts.append(f"===FILE: {rel_path}===\n{content}")
    return "\n\n".join(parts)


_FILE_DELIMITER_RE = re.compile(
    r"===FILE:\s*([^\n=]+?)===\s*\n(.*?)(?=\n===FILE:|\Z)",
    re.DOTALL,
)


def _parse_multi_file_response(raw: str) -> list[tuple[str, str]]:
    """Parse the LLM's multi-file response into [(relative_path, content)] pairs.

    Expected format:
        ===FILE: connector.py===
        <code>
        ===FILE: helpers/utils.py===
        <code>
    """
    results = []
    for match in _FILE_DELIMITER_RE.finditer(raw):
        rel_path = match.group(1).strip()
        content = match.group(2).strip()
        if rel_path and content:
            results.append((rel_path, content))
    return results


# ── Main handler ─────────────────────────────────────────────────────


async def handle_user_prompt(
    session_id: str,
    tenant_id: str,
    prompt: str,
    event_sink: EventSink,
) -> dict[str, Any]:
    """Process a user's freeform prompt to modify generated code.

    Automatically switches between single-file and multi-file restructure mode
    based on the prompt intent.

    Returns dict with status and details.
    """
    from integration.data.catalog import get_service_detail
    from integration.db.database import sessions_collection

    await event_sink("prompt_processing", {"message": "Loading session context..."})

    # ── 1. Load session ──
    try:
        oid = ObjectId(session_id)
    except Exception:
        await event_sink("prompt_error", {"message": f"Invalid session ID: {session_id}"})
        return {"status": "fail", "message": "Invalid session ID"}

    session = await sessions_collection().find_one({"_id": oid, "tenant_id": tenant_id})
    if not session:
        await event_sink("prompt_error", {"message": "Session not found"})
        return {"status": "fail", "message": "Session not found"}

    # Propagate tenant_id to LLM client ContextVar (needed for MCP mode)
    set_llm_tenant_id(tenant_id)

    provider = session.get("provider", "unknown")
    service = session.get("service", "unknown")
    service_slug = session.get("service_slug") or service.replace("-", "_").lower()

    catalog_info = get_service_detail(provider, service)
    service_name = catalog_info.get("display_name", service) if catalog_info else service
    auth_type = catalog_info.get("auth_type", "unknown") if catalog_info else "unknown"

    out_dir = _output_dir(tenant_id, service_slug)
    import re as _re_uph

    _clean_slug_uph = (
        _re_uph.sub(r"_connector$", "", service_slug) if service_slug.endswith("_connector") else service_slug
    )
    package_root = f"{_clean_slug_uph}_connector"

    # ── 2. Route: metadata → restructure → single-file ──
    if _is_metadata_prompt(prompt):
        return await _handle_metadata(
            session_id,
            tenant_id,
            provider,
            service_name,
            service_slug,
            auth_type,
            out_dir,
            event_sink,
            session,
        )

    if _is_restructure_prompt(prompt):
        return await _handle_restructure(
            prompt,
            provider,
            service_name,
            auth_type,
            out_dir,
            package_root,
            event_sink,
        )

    return await _handle_single_file(
        prompt,
        provider,
        service_name,
        auth_type,
        out_dir,
        event_sink,
    )


# ── Metadata generation mode ──────────────────────────────────────────


async def _handle_metadata(
    session_id: str,
    tenant_id: str,
    provider: str,
    service_name: str,
    service_slug: str,
    auth_type: str,
    out_dir: Path,
    event_sink: EventSink,
    session: dict[str, Any],
) -> dict[str, Any]:
    """Generate / regenerate metadata/connector.json using the metadata writing guideline.

    Delegates to handle_generate_metadata() which:
      - Loads _METADATA_SYSTEM_PROMPT from R2 (live-editable)
      - Injects metadata_writing_guideline.md from the global RAG KB
      - Bumps version if connector.json already exists
      - Writes metadata/connector.json to disk
    """
    from integration.services.step_executor import handle_generate_metadata

    await event_sink(
        "prompt_processing",
        {
            "message": "Metadata generation requested — reading guidelines from knowledge base...",
        },
    )

    connector_path = out_dir / "connector.py"
    if not connector_path.exists():
        await event_sink(
            "prompt_error",
            {
                "message": "connector.py not found — run write_connector step first before generating metadata.",
            },
        )
        return {"status": "fail", "message": "connector.py missing"}

    await event_sink(
        "prompt_processing",
        {
            "message": "Generating metadata/connector.json with RAG-backed guidelines...",
        },
    )

    # Build the same context dict that codegen steps use so RAG + step memory work
    context = {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "provider": provider,
        "service_name": service_name,
        "service_slug": service_slug,
        "connector_name": session.get("connector_name", service_name),
        "auth_type": auth_type,
        "sdk_package": session.get("sdk_package", ""),
        "user_prompt": session.get("user_prompt", ""),
        "step_memory": session.get("step_memory", {}),
        "step_type": "generate_metadata",  # ensures correct RAG query routing
    }

    try:

        async def _log(level: str, msg: str) -> None:
            await event_sink("prompt_processing", {"message": msg})

        result = await handle_generate_metadata(
            config={"version": "auto"},  # version bumped automatically inside handler
            context=context,
            log_cb=_log,
        )

        if result.get("status") == "pass":
            metadata = result.get("output", {})
            version = metadata.get("version", "?") if isinstance(metadata, dict) else "?"
            await event_sink(
                "prompt_complete",
                {
                    "message": f"metadata/connector.json generated successfully (v{version})",
                    "modified_files": ["metadata/connector.json"],
                    "metadata_version": version,
                },
            )
            return {
                "status": "pass",
                "message": f"metadata/connector.json v{version} generated",
            }
        await event_sink(
            "prompt_error",
            {
                "message": result.get("output", "Metadata generation failed"),
            },
        )
        return {
            "status": "fail",
            "message": result.get("output", "Metadata generation failed"),
        }

    except Exception as exc:
        logger.error("handle_metadata.error", error=str(exc), session_id=session_id)
        await event_sink("prompt_error", {"message": f"Metadata generation error: {exc}"})
        return {"status": "fail", "message": str(exc)}


# ── Single-file mode ──────────────────────────────────────────────────


async def _handle_single_file(
    prompt: str,
    provider: str,
    service_name: str,
    auth_type: str,
    out_dir: Path,
    event_sink: EventSink,
) -> dict[str, Any]:
    """Modify a single identified file."""

    # Identify target file
    target_file = _identify_target_file(prompt, out_dir)
    await event_sink(
        "prompt_processing",
        {
            "message": f"Target file identified: {target_file}",
            "target_file": target_file,
        },
    )

    file_path = out_dir / target_file
    if not file_path.exists():
        await event_sink(
            "prompt_error",
            {
                "message": f"File not found: {target_file} — run the relevant step first",
            },
        )
        return {"status": "fail", "message": f"File not found: {target_file}"}

    current_code = file_path.read_text(encoding="utf-8")
    full_line_count = len(current_code.splitlines())

    if target_file.startswith("tests/") and full_line_count > 400:
        focused_code = _focused_test_code(current_code, prompt)
        focused_line_count = len(focused_code.splitlines())
        code_for_llm = focused_code
        await event_sink(
            "prompt_processing",
            {
                "message": f"Read {target_file} ({full_line_count} lines → focused to {focused_line_count} lines for LLM)",
            },
        )
    else:
        code_for_llm = current_code
        await event_sink(
            "prompt_processing",
            {
                "message": f"Read {target_file} ({full_line_count} lines)",
            },
        )

    await event_sink(
        "prompt_llm_calling",
        {
            "message": f"Sending {target_file} + your prompt to Gemini...",
        },
    )

    system = (await r2_service.get_step_prompt("USER_MODIFY_PROMPT", USER_MODIFY_PROMPT)).format(
        current_code=code_for_llm,
        user_prompt=prompt,
        provider=provider,
        service_name=service_name,
        auth_type=auth_type,
    )
    # User message: no file names, no "modify" framing — just the instruction
    messages = [
        {
            "role": "user",
            "content": f"Instruction: {prompt}\n\nOutput the transformed Python code now.",
        }
    ]

    _prompt_state: dict = {"last_logged": 0}

    async def _prompt_on_chunk(chars_so_far: int, chunk: str) -> None:
        if chars_so_far - _prompt_state["last_logged"] >= 500:
            _prompt_state["last_logged"] = chars_so_far
            await event_sink(
                "prompt_processing",
                {"message": f"  ⚡ Gemini generating... ({chars_so_far} chars, ~{chars_so_far // 40} lines)"},
            )

    try:
        await event_sink("prompt_processing", {"message": "⚡ Calling Gemini..."})
        code = await call_llm_fix(messages, system=system, max_tokens=32768, on_chunk=_prompt_on_chunk)
        code = _clean_llm_code_response(code)
        line_count = len(code.splitlines()) if code else 0
        await event_sink("prompt_processing", {"message": f"Gemini responded ({line_count} lines)"})
    except Exception as exc:
        await event_sink("prompt_error", {"message": f"LLM call failed: {exc}"})
        return {"status": "fail", "message": str(exc)}

    # ── Retry if Gemini returned non-code ──
    is_non_code = not code or len(code) < 50 or not code.lstrip().startswith(_VALID_PYTHON_STARTS)

    if is_non_code:
        await event_sink(
            "prompt_processing",
            {
                "message": "⚠ Gemini returned non-code response — retrying with explicit code prompt...",
            },
        )
        retry_system = (
            "Output only Python code. No prose. No explanation. No markdown. "
            "First line must start with 'import', 'from', '#', or '\"\"\"'."
        )
        retry_messages = [
            {
                "role": "user",
                "content": (
                    f"Here is a Python code snippet:\n\n"
                    f"```python\n{code_for_llm}\n```\n\n"
                    f"Apply this change: {prompt}\n\n"
                    f"Output ONLY the complete modified Python code. "
                    f"Start immediately with the first import or comment. "
                    f"Do not say anything else."
                ),
            },
        ]
        try:
            code = await call_llm_fix(
                retry_messages,
                system=retry_system,
                max_tokens=32768,
                on_chunk=_prompt_on_chunk,
            )
            code = _clean_llm_code_response(code)
        except Exception as exc:
            await event_sink("prompt_error", {"message": f"LLM retry failed: {exc}"})
            return {"status": "fail", "message": str(exc)}

    if not code or len(code) < 50 or not code.lstrip().startswith(_VALID_PYTHON_STARTS):
        await event_sink(
            "prompt_error",
            {
                "message": f"LLM returned invalid response (not Python code): {code[:120] if code else '(empty)'}",
            },
        )
        return {"status": "fail", "message": "LLM did not return valid Python code"}

    try:
        ast.parse(code)
    except SyntaxError as exc:
        await event_sink(
            "prompt_processing",
            {"message": f"⚠ Syntax error at line {exc.lineno} (likely truncated) — asking Gemini to complete..."},
        )
        lines = code.splitlines()
        clean_lines = lines[: max(0, (exc.lineno or len(lines)) - 1)]
        clean_prefix = "\n".join(clean_lines)
        await event_sink(
            "prompt_processing",
            {
                "message": f"⚠ Code truncated at line {exc.lineno} — asking Gemini to continue from line {len(clean_lines)}..."
            },
        )
        continuation_messages = [
            {
                "role": "user",
                "content": (
                    f"A Python file was truncated at line {exc.lineno}. "
                    f"Here is everything up to the truncation:\n\n"
                    f"```python\n{clean_prefix}\n```\n\n"
                    f"Continue writing from exactly where it was cut off. "
                    f"Close all open parentheses, brackets, classes and functions. "
                    f"Output ONLY the continuation — do NOT repeat existing lines."
                ),
            },
        ]
        continuation_system = (
            "Output only Python code — the continuation of a truncated file. "
            "No prose. No markdown. Do NOT repeat existing lines. "
            "Close all open indentation blocks properly."
        )
        continuation = await call_llm_fix(
            continuation_messages,
            system=continuation_system,
            max_tokens=32768,
            on_chunk=_prompt_on_chunk,
        )
        continuation = _clean_llm_code_response(continuation)
        code = clean_prefix + "\n" + continuation
        try:
            ast.parse(code)
            await event_sink(
                "prompt_processing",
                {"message": f"✅ Code completed ({len(code.splitlines())} lines)"},
            )
        except SyntaxError as exc2:
            await event_sink("prompt_error", {"message": f"Still broken after continuation: {exc2}"})
            return {"status": "fail", "message": f"Syntax error: {exc2}"}

    file_path.write_text(code, encoding="utf-8")
    quality = analyze_file(str(file_path))
    line_count = quality.get("line_count", len(code.splitlines()))
    score = quality.get("quality_score", 0)

    await event_sink(
        "prompt_file_updated",
        {
            "file": target_file,
            "line_count": line_count,
            "quality_score": score,
        },
    )

    if target_file == "connector.py":
        context = {
            "service_name": service_name,
            "provider": provider,
            "auth_type": auth_type,
        }
        actual_class = _sync_init_with_connector(out_dir, context)
        if actual_class:
            await event_sink(
                "prompt_processing",
                {
                    "message": f"__init__.py synced with class: {actual_class}",
                },
            )

    await event_sink(
        "prompt_complete",
        {
            "message": f"✓ {target_file} updated ({line_count} lines, score: {score})",
            "files_modified": [target_file],
        },
    )
    return {
        "status": "pass",
        "file": target_file,
        "line_count": line_count,
        "quality_score": score,
    }


# ── Multi-file restructure mode ───────────────────────────────────────


async def _handle_restructure(
    prompt: str,
    provider: str,
    service_name: str,
    auth_type: str,
    out_dir: Path,
    package_root: str,
    event_sink: EventSink,
) -> dict[str, Any]:
    """Restructure the full connector package across helpers/client."""

    await event_sink(
        "prompt_processing",
        {
            "message": "🔀 Restructure mode detected — reading all package files...",
        },
    )

    # Read all current source files (excludes tests/)
    current_files = _read_package_files(out_dir)
    if not current_files:
        await event_sink(
            "prompt_error",
            {"message": "No source files found — run scaffold and write_connector first"},
        )
        return {"status": "fail", "message": "No source files to restructure"}

    await event_sink(
        "prompt_processing",
        {
            "message": f"Read {len(current_files)} package file(s): {', '.join(current_files.keys())}",
        },
    )

    # Ensure subdirectory stubs exist so LLM can reference them
    for subdir in ["helpers", "client"]:
        sub = out_dir / subdir
        sub.mkdir(exist_ok=True)
        init_file = sub / "__init__.py"
        if not init_file.exists():
            init_file.write_text(f'"""{subdir.replace("_", " ").title()} package."""\n', encoding="utf-8")

    current_files_block = _build_current_files_block(current_files)

    await event_sink(
        "prompt_llm_calling",
        {
            "message": f"Sending {len(current_files)} files to Gemini for restructuring...",
        },
    )

    system = (await r2_service.get_step_prompt("USER_RESTRUCTURE_PROMPT", USER_RESTRUCTURE_PROMPT)).format(
        user_prompt=prompt,
        provider=provider,
        service_name=service_name,
        auth_type=auth_type,
        package_root=package_root,
        current_files_block=current_files_block,
    )
    messages = [
        {
            "role": "user",
            "content": (
                f"Restructure the {service_name} connector package according to this instruction: {prompt}\n\n"
                "Output each modified/created file using the ===FILE: path=== delimiter format. "
                "Only include files you are changing or creating."
            ),
        }
    ]

    _restructure_state: dict = {"last_logged": 0}

    async def _restructure_on_chunk(chars_so_far: int, chunk: str) -> None:
        if chars_so_far - _restructure_state["last_logged"] >= 500:
            _restructure_state["last_logged"] = chars_so_far
            await event_sink(
                "prompt_processing",
                {"message": f"  ⚡ Gemini generating... ({chars_so_far} chars, ~{chars_so_far // 40} lines)"},
            )

    try:
        await event_sink(
            "prompt_processing",
            {"message": f"⚡ Calling Gemini to restructure {len(current_files)} files..."},
        )
        raw = await call_llm_fix(
            messages,
            system=system,
            max_tokens=16000,
            expect_code=False,
            on_chunk=_restructure_on_chunk,
        )
        await event_sink(
            "prompt_processing",
            {"message": f"Gemini responded ({len(raw.splitlines())} lines)"},
        )
    except Exception as exc:
        await event_sink("prompt_error", {"message": f"LLM call failed: {exc}"})
        return {"status": "fail", "message": str(exc)}

    # Parse multi-file response
    parsed_files = _parse_multi_file_response(raw)
    if not parsed_files:
        # Retry with an explicit format reminder before giving up
        logger.warning(
            "restructure.format_not_detected",
            response_preview=raw[:400] if raw else "(empty)",
            response_length=len(raw) if raw else 0,
        )
        await event_sink(
            "prompt_processing",
            {
                "message": "⚠ Multi-file format not detected — retrying with format reminder...",
            },
        )
        retry_messages = [
            *messages,
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "Your previous response did not use the required ===FILE: format.\n"
                "You MUST output ONLY file blocks, each starting with ===FILE: <path>===\n"
                "Do NOT include any prose, explanation, or markdown fences.\n"
                "Begin your response immediately with ===FILE:",
            },
        ]
        try:
            raw = await call_llm_fix(
                retry_messages,
                system=system,
                max_tokens=16000,
                expect_code=False,
                on_chunk=_restructure_on_chunk,
            )
        except Exception as exc:
            await event_sink("prompt_error", {"message": f"LLM retry failed: {exc}"})
            return {"status": "fail", "message": str(exc)}

        parsed_files = _parse_multi_file_response(raw)
        if not parsed_files:
            logger.error(
                "restructure.format_not_detected_after_retry",
                response_preview=raw[:400] if raw else "(empty)",
            )
            await event_sink(
                "prompt_error",
                {"message": "LLM did not return valid restructure output after retry"},
            )
            return {"status": "fail", "message": "No valid file blocks in LLM response"}

    await event_sink(
        "prompt_processing",
        {
            "message": f"Parsed {len(parsed_files)} file(s) from Gemini response — writing...",
        },
    )

    # Remember the original flat source files so we can clean them up afterwards.
    # Only track top-level .py files that are NOT essential scaffolding.
    _ESSENTIAL_FILES = {"connector.py", "__init__.py", "config.py", "auth.py"}
    old_flat_files = {
        rel for rel in current_files if "/" not in rel and rel.endswith(".py") and rel not in _ESSENTIAL_FILES
    }

    written: list[str] = []
    errors: list[str] = []

    for rel_path, content in parsed_files:
        # Sanitise path — must not escape out_dir
        clean_path = rel_path.lstrip("/").replace("..", "")
        file_path = out_dir / clean_path
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Validate Python files
        if clean_path.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as exc:
                await event_sink(
                    "prompt_processing",
                    {
                        "message": f"  ⚠ {clean_path}: syntax error ({exc}) — writing anyway for inspection",
                    },
                )
                errors.append(clean_path)

        file_path.write_text(content, encoding="utf-8")
        quality = analyze_file(str(file_path)) if clean_path.endswith(".py") else {}
        line_count = quality.get("line_count", content.count("\n") + 1)
        await event_sink(
            "prompt_file_updated",
            {
                "file": clean_path,
                "line_count": line_count,
                "quality_score": quality.get("quality_score", 0),
            },
        )
        await event_sink(
            "prompt_processing",
            {
                "message": f"  ✓ {clean_path} ({line_count} lines)",
            },
        )
        written.append(clean_path)

    # ── Clean up superseded flat source files ──────────────────────────
    # Any old top-level source file that was NOT included in the new structure
    # (i.e. its logic has been moved into helpers/ or client/)
    # should be deleted so View Code shows a clean, restructured tree.
    # We only delete files that were present before AND are not in `written`
    # (which would mean they were explicitly rewritten by the LLM).
    deleted: list[str] = []
    subdirs_written = any("/" in w for w in written)  # at least one subdir file was created
    if subdirs_written:
        for old_rel in old_flat_files:
            if old_rel not in written:
                old_path = out_dir / old_rel
                if old_path.exists():
                    try:
                        old_path.unlink()
                        deleted.append(old_rel)
                        await event_sink(
                            "prompt_processing",
                            {
                                "message": f"  🗑 Removed superseded flat file: {old_rel}",
                            },
                        )
                    except Exception as exc:
                        logger.warning("restructure.cleanup_failed", file=old_rel, error=str(exc))

    # Sync __init__.py if connector.py was regenerated
    if "connector.py" in written:
        context = {
            "service_name": service_name,
            "provider": provider,
            "auth_type": auth_type,
        }
        actual_class = _sync_init_with_connector(out_dir, context)
        if actual_class:
            await event_sink(
                "prompt_processing",
                {
                    "message": f"__init__.py synced with class: {actual_class}",
                },
            )

    status = "fail" if len(errors) == len(written) else "pass"
    summary = f"✓ Restructure complete — {len(written)} file(s) written, {len(deleted)} superseded file(s) removed: {', '.join(written)}"
    if errors:
        summary += f" | ⚠ {len(errors)} file(s) had syntax errors (written for inspection)"

    await event_sink(
        "prompt_complete",
        {
            "message": summary,
            "files_modified": written,
        },
    )

    return {"status": status, "files_modified": written, "errors": errors}
