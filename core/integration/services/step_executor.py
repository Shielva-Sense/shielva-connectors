"""Integration Builder — Step executor.

Handles execution of each plan step type:
  install_deps, configure_auth, scaffold_code, write_connector, smoke_test, write_tests, run_tests
"""

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

import structlog

from integration.core.config import settings
from integration.prompts.codegen_prompt import (
    AUTH_CONFIG_PROMPT,
    CONNECTOR_SYSTEM_PROMPT,
    FIX_CODE_PROMPT,
    FIX_CONNECTOR_FOR_TESTS_PROMPT,
    FIX_TESTS_PROMPT,
    SCAFFOLD_CONFIG_TEMPLATE,
    SCAFFOLD_CONFIG_TEMPLATE_APIKEY,
    SCAFFOLD_CONFIG_TEMPLATE_OAUTH,
    SCAFFOLD_INIT_TEMPLATE,
    TEST_MODULE_SYSTEM_PROMPT,
    TEST_RULES_GENERATION_PROMPT,
    TEST_SYSTEM_PROMPT,
)
from integration.prompts.planning_prompt import BASE_CONNECTOR_INTERFACE
from integration.services.code_quality import analyze_file
from integration.services.llm_client import call_llm, call_llm_fix, call_llm_tests
from integration.services import knowledge_service
from integration.services import r2_service
from integration.services import r2_service


async def _get_prompt(name: str, fallback: str) -> str:
    """Fetch a step prompt from R2 (hot cache → R2 → local file → fallback constant).

    This is the single entry-point for all LLM prompt loading in step_executor.
    R2 is the live source of truth; the local Python constant is the fallback
    so the system never breaks even when R2 is unavailable.
    """
    return await r2_service.get_step_prompt(name, fallback)

logger = structlog.get_logger(__name__)

# Type alias for the async log callback
LogCallback = Optional[Callable[[str, str], Coroutine[Any, Any, None]]]


# Module-level in-memory RAG cache keyed by (query_prefix, tenant_id, provider, service).
# Scoped to the process lifetime — cleared on service restart.
# P4: Eliminates redundant MCP calls when multiple steps use similar queries.
_rag_cache: dict = {}


def _extract_connector_facts(context: dict) -> dict:
    """Extract concrete facts from connector.py via AST for context-aware RAG queries.

    Returns a dict with keys: class_name, auth_type, auth_uri, token_uri,
    methods, sdk_imports, install_fields, api_endpoints.
    All values are strings or lists — empty string / empty list if not found.
    """
    tenant_id = context.get("tenant_id", "")
    service_slug = context.get("service_slug", "") or context.get("service_name", "").lower().replace(" ", "_")
    facts: dict = {
        "class_name": context.get("connector_name", ""),
        "auth_type": context.get("auth_type", ""),
        "auth_uri": "",
        "token_uri": "",
        "methods": [],
        "sdk_imports": [],
        "install_fields": [],
        "api_endpoints": [],
    }

    if not tenant_id or not service_slug:
        return facts

    connector_path = _output_dir(tenant_id, service_slug) / "connector.py"
    if not connector_path.exists():
        return facts

    try:
        source = connector_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception:
        return facts

    for node in ast.walk(tree):
        # Extract class-level attributes (AUTH_TYPE, AUTH_URI, TOKEN_URI)
        if isinstance(node, ast.ClassDef) and "Connector" in node.name:
            if not facts["class_name"]:
                facts["class_name"] = node.name
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for tgt in item.targets:
                        if isinstance(tgt, ast.Name):
                            val = ""
                            if isinstance(item.value, ast.Constant):
                                val = str(item.value.value)
                            if tgt.id == "AUTH_TYPE" and val:
                                facts["auth_type"] = val
                            elif tgt.id == "AUTH_URI" and val:
                                facts["auth_uri"] = val
                            elif tgt.id == "TOKEN_URI" and val:
                                facts["token_uri"] = val
                # Collect public method names
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not item.name.startswith("_"):
                        facts["methods"].append(item.name)

        # Extract SDK imports (third-party packages — not stdlib, not shared/connector)
        if isinstance(node, ast.Import):
            for alias in node.names:
                pkg = alias.name.split(".")[0]
                if pkg not in ("os", "re", "json", "datetime", "typing", "asyncio",
                               "logging", "pathlib", "enum", "abc", "http", "urllib"):
                    facts["sdk_imports"].append(pkg)
        if isinstance(node, ast.ImportFrom) and node.module:
            pkg = node.module.split(".")[0]
            if pkg not in ("os", "re", "json", "datetime", "typing", "asyncio",
                           "logging", "pathlib", "enum", "abc", "http", "urllib",
                           "shared", "connector", "__future__"):
                facts["sdk_imports"].append(pkg)

        # Extract install_fields from self.config.get("field_name", ...)
        if isinstance(node, ast.Call):
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "config"):
                if node.args and isinstance(node.args[0], ast.Constant):
                    field = str(node.args[0].value)
                    if field not in facts["install_fields"]:
                        facts["install_fields"].append(field)

        # Extract API endpoint strings from httpx/requests calls and f-strings
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value
            if val.startswith("https://") and len(val) > 20 and len(val) < 200:
                facts["api_endpoints"].append(val[:100])

    # Deduplicate
    facts["sdk_imports"] = list(dict.fromkeys(facts["sdk_imports"]))[:6]
    facts["api_endpoints"] = list(dict.fromkeys(facts["api_endpoints"]))[:4]

    return facts


def _build_context_aware_rag_query(step_type: str, facts: dict, service_name: str,
                                    connector_name: str, user_prompt: str) -> str:
    """Build a targeted RAG query from actual connector facts rather than generic templates.

    Each step type focuses on what the retriever needs to surface:
    - write_connector: SDK name + auth flow + service-specific API details
    - write_tests:     actual class name + method signatures for mock setup
    - configure_auth:  exact auth endpoints + token type
    - generate_metadata: install_fields found in code + auth_type
    - scaffold_code:   SDK package + base class structure
    - install_deps:    SDK package names
    Fallback: best-effort from any available facts.
    """
    auth_type = facts.get("auth_type", "") or ""
    sdk = " ".join(facts.get("sdk_imports", []))
    methods = " ".join(facts.get("methods", []))
    auth_uri = facts.get("auth_uri", "") or ""
    token_uri = facts.get("token_uri", "") or ""
    fields = " ".join(facts.get("install_fields", []))
    class_name = facts.get("class_name", "") or connector_name
    endpoints = " ".join(facts.get("api_endpoints", []))

    if step_type == "write_connector":
        parts = [service_name, sdk or connector_name, auth_type or "authentication"]
        if auth_uri:
            parts.append(auth_uri.split("/")[2] if "/" in auth_uri else auth_uri)  # domain only
        parts.extend(["connector", "sync", "install"])
        return " ".join(filter(None, parts))

    if step_type == "write_tests":
        parts = [class_name, service_name, "pytest"]
        if methods:
            parts.append(methods)
        if sdk:
            parts.append(sdk)
        parts.extend(["mock", "fixtures", "test"])
        return " ".join(filter(None, parts))

    if step_type == "configure_auth":
        parts = [service_name, auth_type or "OAuth2"]
        if auth_uri:
            # Use the domain of the auth URI as a concrete search term
            domain = auth_uri.split("/")[2] if auth_uri.startswith("http") else auth_uri
            parts.append(domain)
        if token_uri:
            domain = token_uri.split("/")[2] if token_uri.startswith("http") else token_uri
            parts.append(domain)
        parts.extend(["credentials", "token", "client_id", "client_secret"])
        return " ".join(filter(None, parts))

    if step_type == "generate_metadata":
        parts = [service_name, "connector.json", "install_fields"]
        if fields:
            parts.append(fields)
        if auth_type:
            parts.append(auth_type)
        parts.extend(["required", "field_type", "metadata"])
        return " ".join(filter(None, parts))

    if step_type == "scaffold_code":
        parts = [service_name, sdk or connector_name, auth_type or "authentication",
                 "BaseConnector", "scaffold", "structure"]
        return " ".join(filter(None, parts))

    if step_type == "install_deps":
        parts = [service_name, sdk or connector_name, "Python", "packages", "requirements"]
        return " ".join(filter(None, parts))

    if step_type in ("fix_connector", "fix_tests"):
        parts = [class_name, service_name, sdk or "", methods or ""]
        if auth_type:
            parts.append(auth_type)
        return " ".join(filter(None, parts))

    # Generic fallback
    parts = [class_name or connector_name, service_name, sdk, auth_type, user_prompt[:80]]
    return " ".join(filter(None, parts)).strip()


async def _inject_rag_context(system: str, context: dict) -> str:
    """Append RAG knowledge context to a system prompt if available.

    Queries the MCP RAG knowledge base for relevant chunks based on the
    current connector context. Queries are built dynamically from actual
    connector.py content (via AST) rather than static step-type templates,
    so the retriever surfaces the most relevant SDK/code chunks for this
    specific connector.

    - P2: Context-aware queries derived from AST-extracted facts
    - P4: In-memory cache per (query, tenant, provider, service) — avoids
           re-querying MCP on 5-7 similar calls within a single codegen run.
    """
    tenant_id = context.get("tenant_id", "")
    provider = context.get("provider", "")
    service_name = context.get("service_name", "")
    user_prompt = context.get("user_prompt", "")
    connector_name = context.get("connector_name", "")
    step_type = context.get("step_type", "")

    if not tenant_id:
        return system

    # Skip RAG for steps that run before connector.py exists — the connector KB is empty
    # so any MCP call just wastes ~30s and returns nothing (or global guidelines noise).
    _SKIP_RAG_STEPS = {"install_deps", "scaffold_code"}
    if step_type in _SKIP_RAG_STEPS:
        logger.debug("rag_context.skipped_early_step", step_type=step_type)
        return system

    # Extract concrete facts from connector.py (if it exists yet)
    facts = _extract_connector_facts(context)
    logger.debug(
        "rag_context.facts_extracted",
        step_type=step_type,
        auth_type=facts.get("auth_type"),
        class_name=facts.get("class_name"),
        sdk_imports=facts.get("sdk_imports"),
        methods=facts.get("methods"),
        install_fields=facts.get("install_fields"),
    )

    # Build a context-aware query from real code facts
    query = _build_context_aware_rag_query(
        step_type=step_type,
        facts=facts,
        service_name=service_name,
        connector_name=connector_name,
        user_prompt=user_prompt,
    )

    if not query:
        return system

    # P4: Check in-memory cache
    cache_key = (query[:100], tenant_id, provider, service_name)
    if cache_key in _rag_cache:
        knowledge_chunks = _rag_cache[cache_key]
        logger.debug("rag_context.cache_hit", step_type=step_type, query_preview=query[:60])
    else:
        try:
            knowledge_chunks = await knowledge_service.query_knowledge(
                query=query,
                tenant_id=tenant_id,
                provider=provider,
                service=service_name,
                top_k=10,
            )
            _rag_cache[cache_key] = knowledge_chunks
            logger.debug(
                "rag_context.queried",
                step_type=step_type,
                query_preview=query[:60],
                chunks_found=bool(knowledge_chunks),
            )
        except Exception as exc:
            logger.warning("rag_context.query_failed", error=str(exc), query=query[:100])
            return system

    if knowledge_chunks:
        return (
            system
            + "\n\n## Knowledge Context (from uploaded guidelines and SDK docs)\n"
            + "Use the following knowledge when generating code:\n\n"
            + knowledge_chunks
        )

    return system


def _make_gemini_progress_cb(log_cb: LogCallback, label: str = "Gemini"):
    """Return an on_chunk callback that streams live Gemini output line-by-line.

    Emits each complete line as it arrives — no noisy char-count heartbeats.
    """
    state = {"line_buf": ""}

    async def _on_chunk(chars_so_far: int, chunk: str):
        if log_cb is None:
            return
        try:
            state["line_buf"] += chunk
            while "\n" in state["line_buf"]:
                line, state["line_buf"] = state["line_buf"].split("\n", 1)
                line = line.rstrip()
                if line:
                    await log_cb("info", f"  💭 {line}")
        except Exception:
            pass

    return _on_chunk


def _fix_connector_import(code: str, class_name: str) -> str:
    """Post-process generated test code to fix/ensure correct connector import and patch paths.

    Handles three cases:
      1. Wrong module path:  from google_adsense_connector.connector import X  → from connector import X
      2. Missing import entirely: class_name used in code but never imported  → inject import after last 'import' line
      3. Wrong patch targets: patch('google_adsense.connector.X')  → patch('connector.X')
    """
    import re

    correct_import = f"from connector import {class_name}"
    fixed_lines = []
    last_import_idx = -1

    for i, line in enumerate(code.splitlines()):
        stripped = line.strip()

        # ── Fix: `from <something>.connector import <ClassName>` ──
        if (
            stripped.startswith("from ")
            and f"import {class_name}" in stripped
            and ".connector import" in stripped
            and not stripped.startswith("from connector import")
        ):
            fixed_lines.append(correct_import)
            last_import_idx = len(fixed_lines) - 1
            continue

        # ── Fix: `from <wrong_package> import <ClassName>` ──
        if (
            stripped.startswith("from ")
            and f"import {class_name}" in stripped
            and ".connector" not in stripped
            and not stripped.startswith("from connector import")
            and not stripped.startswith("from shared")
            and class_name in stripped
        ):
            fixed_lines.append(correct_import)
            last_import_idx = len(fixed_lines) - 1
            continue

        # Track last import line position (to inject missing import after it)
        if stripped.startswith(("import ", "from ")) and not stripped.startswith("from connector import"):
            last_import_idx = len(fixed_lines)

        # ── Fix: correct import already present ──
        if stripped == correct_import:
            last_import_idx = len(fixed_lines)

        # ── Fix: patch/mocker.patch string targets with wrong module prefix ──
        line = re.sub(
            r"""(['"])([a-zA-Z0-9_]+\.)*connector\.([a-zA-Z0-9_.]+)\1""",
            lambda m: f"{m.group(1)}connector.{m.group(3)}{m.group(1)}",
            line,
        )
        fixed_lines.append(line)

    # ── Ensure import exists — inject if class_name is used but never imported ──
    result = "\n".join(fixed_lines)
    if class_name in result and correct_import not in result:
        inject_at = last_import_idx + 1 if last_import_idx >= 0 else 0
        fixed_lines.insert(inject_at, correct_import)
        result = "\n".join(fixed_lines)

    # ── Strip hallucinated BaseConnector methods Gemini invents ─────────────
    # These methods don't exist on BaseConnector: save_config, _save_config,
    # save_token, _save_token, etc. Remove any line that references them.
    # IMPORTANT: when stripping a `with patch.object(..., 'save_config', ...):`
    # line, also remove its indented body — otherwise the orphaned indented code
    # causes IndentationError and every test fails to collect.
    def _has_hallucinated_method(line: str) -> bool:
        for bad in _HALLUCINATED_METHODS:
            if bad + "(" in line or f"'{bad}'" in line or f'"{bad}"' in line:
                return True
        return False

    src_lines = result.splitlines()
    filtered: list[str] = []
    skip_indent: int | None = None  # if set, skip lines more indented than this level
    for line in src_lines:
        if _has_hallucinated_method(line):
            # If this line opens a block (ends with ':'), record its indent level
            # so we can skip the orphaned indented body below it.
            stripped = line.rstrip()
            if stripped.endswith(":"):
                indent = len(line) - len(line.lstrip())
                skip_indent = indent
            # Drop the hallucinated line itself
            continue
        if skip_indent is not None:
            indent = len(line) - len(line.lstrip())
            if line.strip() == "" or indent > skip_indent:
                # Part of the stripped block's body — drop it
                continue
            else:
                # Back to original indentation level — stop skipping
                skip_indent = None
        filtered.append(line)
    return "\n".join(filtered)


def _strip_hallucinated_imports(code: str, connector_path: "Path") -> str:
    """Remove import names that don't actually exist in connector.py or its sub-modules.

    Gemini frequently hallucinates class/exception names like 'GmailConnectorErrorError'
    (double suffix) or 'GmailApiClientError' that don't exist in the connector module.
    This function cross-references `from connector import X, Y, Z` lines against the
    actual names available in connector.py — including names re-exported via relative
    imports (e.g. `from .exceptions import GmailConnectorError`) and sub-module files.

    Runs AFTER _fix_connector_import so module paths are already correct.
    """
    if not connector_path.exists():
        return code

    out_dir = connector_path.parent

    # ── Collect ALL names actually available from `from connector import X` ──
    real_names: set = set()

    def _collect_from_file(path: "Path") -> None:
        """Add all defined + re-exported names from a Python source file."""
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    real_names.add(node.name)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    real_names.add(node.name)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            real_names.add(target.id)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    # Capture re-exports: `from .exceptions import X` in connector.py
                    if isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            real_names.add(alias.asname or alias.name)
        except Exception:
            pass

    # 1. Scan connector.py itself
    _collect_from_file(connector_path)

    # 2. Scan all sibling .py files (exceptions.py, config.py, __init__.py, etc.)
    #    These are importable via `from connector import X` when connector.py re-exports them.
    for py_file in out_dir.glob("*.py"):
        if py_file != connector_path:
            _collect_from_file(py_file)

    # 3. Scan sub-packages (helpers/, client/, etc.)
    for sub_dir in ["helpers", "client", "utils"]:
        sub_path = out_dir / sub_dir
        if sub_path.is_dir():
            for py_file in sub_path.glob("*.py"):
                _collect_from_file(py_file)

    if not real_names:
        return code

    fixed_lines = []
    for line in code.splitlines():
        stripped = line.strip()
        # Only process `from connector import ...` lines
        if stripped.startswith("from connector import "):
            after_import = stripped[len("from connector import "):]
            # Parse the imported names (handle parentheses and trailing commas)
            names = [n.strip().rstrip(",") for n in after_import.replace("(", "").replace(")", "").split(",")]
            valid_names = [n for n in names if n and n in real_names]
            if valid_names:
                indent = line[: len(line) - len(line.lstrip())]
                fixed_lines.append(f"{indent}from connector import {', '.join(valid_names)}")
            # else: all names were hallucinated — drop the entire line
            continue
        fixed_lines.append(line)

    return "\n".join(fixed_lines)


def _build_step_memory_summary(context: dict) -> str:
    """Convert the live step_memory dict into a human-readable summary for LLM prompts.

    This gives every step handler the same 'memory' I have — what was built, what failed,
    what packages are installed, what methods exist, what tests cover.
    """
    mem = context.get("step_memory", {})
    if not mem:
        return "  No step history yet — this is the first step."

    lines = []
    if mem.get("completed_steps"):
        lines.append(f"  ✅ Completed steps: {', '.join(mem['completed_steps'])}")
    if mem.get("installed_packages"):
        lines.append(f"  📦 Installed packages: {', '.join(mem['installed_packages'][:10])}")
    if mem.get("connector_class_name"):
        lines.append(f"  🔌 Connector class: {mem['connector_class_name']}")
    if mem.get("connector_methods"):
        lines.append(f"  🔧 Connector public methods: {', '.join(mem['connector_methods'])}")
    if mem.get("test_methods_covered"):
        lines.append(f"  🧪 Test functions written: {len(mem['test_methods_covered'])} ({', '.join(mem['test_methods_covered'][:5])}{'...' if len(mem['test_methods_covered']) > 5 else ''})")
    if mem.get("last_test_passed") or mem.get("last_test_failed"):
        lines.append(f"  📊 Last test run: {mem['last_test_passed']} passed, {mem['last_test_failed']} failed")
    if mem.get("errors_encountered"):
        lines.append(f"  ❌ Errors so far: {' | '.join(mem['errors_encountered'][-3:])}")  # last 3
    if mem.get("fix_attempts"):
        for stype, cnt in mem["fix_attempts"].items():
            lines.append(f"  🔁 Fix attempts on {stype}: {cnt}")

    return "\n".join(lines) if lines else "  No significant events yet."


def _get_ctx(context: dict, key: str, default: str = "") -> str:
    """Safe context getter that always returns a string."""
    val = context.get(key, default)
    return str(val) if val is not None else default


def _llm_label() -> str:
    """Return a human-readable label for the active TEST_LLM_MODE + model.

    Reads settings at call-time so it reflects live .env values without
    requiring a restart (useful in tests / hot-reloads).

    Examples:
      gemini  → "Gemini (gemini-2.0-flash)"
      kimi    → "DeepSeek (deepseek-chat)"  or "Kimi (kimi-k2-…)"
      cli     → "Claude CLI (claude-sonnet-…)"
      api     → "Claude API (claude-sonnet-…)"
    """
    mode = settings.TEST_LLM_MODE.lower()
    if mode == "gemini":
        return f"Gemini ({settings.GEMINI_MODEL})"
    if mode == "kimi":
        model = settings.KIMI_MODEL
        base = settings.KIMI_BASE_URL
        provider = "DeepSeek" if "deepseek" in base.lower() else "Kimi"
        return f"{provider} ({model})"
    if mode in ("cli", "api"):
        return f"Claude {mode.upper()} ({settings.LLM_MODEL})"
    return mode  # fallback: just echo the mode name


def _output_dir(tenant_id: str, service_slug: str) -> Path:
    """Return the directory where generated code is written.

    Path: {GENERATED_CODE_DIR}/{tenant_id}/{service_slug}_connector/
    The directory IS the Python package root — no extra wrapper needed.
    session_id is NOT in the path — same tenant+service always maps to the same directory,
    preventing duplicate folders across sessions.

    Strips '_connector' from the slug before appending it to avoid double-suffix dirs.
    Handles two cases:
      1. slug ends with '_connector'           → shielva_gmail_connector      → shielva_gmail_connector
      2. slug has '_connector' before the hash → shielva_gmail_connector_cb03d1 → shielva_gmail_cb03d1
    Both produce a clean '{name}_connector' directory name.
    """
    import re as _re
    base = Path(settings.GENERATED_CODE_DIR).resolve()
    # Case 1: trailing _connector (no hash suffix)
    # Case 2: _connector immediately before a 6-char hex hash at end of slug (legacy sessions)
    clean_slug = _re.sub(r'_connector(_[a-f0-9]{6}$|$)', r'\1', service_slug)
    # Local disk is always flat: GENERATED_CODE_DIR/{slug}_connector/
    # Tenant/app scoping lives in R2 keys, not on the local filesystem.
    return base / f"{clean_slug}_connector"


async def _load_test_guidelines(out_dir: Path, provider: str, service_slug: str) -> str:
    """Load connector-specific test guidelines for this service.

    Priority:
      1. Local disk: out_dir/test_guidelines.md  (written by generate_test_guidelines step)
      2. R2 fallback via r2_service.get_test_guidelines()

    Returns empty string if not found or too short to be meaningful (< 300 chars).
    """
    _MIN_CHARS = 300

    # 1. Local disk (fastest — always check first)
    local_path = out_dir / "test_guidelines.md"
    if local_path.exists():
        raw = local_path.read_text(encoding="utf-8")
        if len(raw.strip()) >= _MIN_CHARS:
            return raw

    # 2. R2 fallback
    try:
        raw = await r2_service.get_test_guidelines(provider, service_slug)
        if raw and len(raw.strip()) >= _MIN_CHARS:
            return raw
    except Exception:
        pass

    return ""


# Root of shielva-connectors/ (two levels up from integration/services/)
_CONNECTORS_ROOT = Path(__file__).resolve().parent.parent.parent

# Site-packages of the current interpreter — injected BEFORE _CONNECTORS_ROOT in PYTHONPATH
# so that installed packages (e.g. PyGithub's `github`) always take precedence over any
# local directory at _CONNECTORS_ROOT that shares the same name.
import sysconfig as _sysconfig
import site as _site
_SITE_PACKAGES = _sysconfig.get_paths().get("purelib", "")
# Also include user site-packages (pip install --user puts packages there)
_USER_SITE = _site.getusersitepackages() if hasattr(_site, "getusersitepackages") else ""

# Shared venv site-packages (Python 3.13) — takes precedence so connector subprocesses
# use the correct Python version and pre-installed common deps
try:
    from integration.services.shared_venv import VENV_DIR as _VENV_DIR
    import sysconfig as _venv_sysconfig
    _VENV_SITE_PACKAGES = str(_VENV_DIR / "lib" / "python3.13" / "site-packages")
except Exception:
    _VENV_SITE_PACKAGES = ""


def _append_timeline(
    out_dir: Path,
    session_id: str,
    step_title: str,
    step_index: int,
    status: str,
    duration_ms: float,
) -> None:
    """Append a completed-step entry to ImplementationTimeline.md (sync, called from async context)."""
    timeline_path = out_dir / "ImplementationTimeline.md"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    icon = "✅" if status == "pass" else "❌"
    line = f"- {icon} Step {step_index + 1}: {step_title} — {status} ({duration_ms:.0f}ms) @ {now}\n"

    out_dir.mkdir(parents=True, exist_ok=True)
    if not timeline_path.exists():
        service_label = out_dir.name.replace("_", " ").title()
        header = f"# {service_label} — Implementation Timeline\n\n"
        timeline_path.write_text(header, encoding="utf-8")

    content = timeline_path.read_text(encoding="utf-8")
    session_marker = f"### Session: {session_id}"
    if session_marker not in content:
        content += f"\n{session_marker}\n"
    content += line
    timeline_path.write_text(content, encoding="utf-8")


async def _emit(log_cb: LogCallback, level: str, msg: str):
    if log_cb:
        await log_cb(level, msg)


# ── File integrity validation ────────────────────────────────────────

_VALID_PYTHON_STARTS = ("import ", "from ", "#", '"""', "class ", "def ", "async ")

# Maximum lines of test code to send to the LLM for fix operations.
# Sending the full file (1000+ lines) causes CLI timeouts; we focus on failing tests.
_MAX_FIX_TEST_LINES = 600  # 15 min CLI timeout supports larger focused test code


def _extract_failing_names(error_details: str) -> List[str]:
    """Parse pytest output and return unique failing test class/function names.

    Matches patterns like:
      FAILED tests/test_connector.py::TestFoo::test_bar
      FAILED tests/test_connector.py::test_bar
    Returns a deduped list of class + function names to look up in the AST.
    """
    names: List[str] = []
    for m in re.finditer(r"FAILED\s+\S+::(\w+)(?:::(\w+))?", error_details):
        if m.group(2):
            names.append(m.group(1))  # class name
            names.append(m.group(2))  # function name
        else:
            names.append(m.group(1))  # standalone function
    return list(dict.fromkeys(names))  # deduplicate, preserve order


def _focused_test_code(full_code: str, error_details: str) -> str:
    """Return a smaller version of a test file containing only failing tests.

    Strategy:
    1. Parse failing test names from pytest output.
    2. Walk the AST to find matching ClassDef / FunctionDef nodes.
    3. Reconstruct: all imports (lines before first class/function) + matching nodes.
    4. Falls back to the full code if parsing fails or no names found.

    This keeps prompts under ~400 lines so the LLM can respond within 10 minutes.
    """
    if len(full_code.splitlines()) <= _MAX_FIX_TEST_LINES:
        return full_code  # small enough — send as-is

    failing_names = _extract_failing_names(error_details)
    if not failing_names:
        # No structured names found — truncate to first 400 lines as a fallback
        lines = full_code.splitlines()
        return "\n".join(lines[:_MAX_FIX_TEST_LINES]) + "\n# ... (truncated)"

    try:
        tree = ast.parse(full_code)
    except SyntaxError:
        lines = full_code.splitlines()
        return "\n".join(lines[:_MAX_FIX_TEST_LINES]) + "\n# ... (truncated)"

    lines = full_code.splitlines()
    failing_name_set = set(failing_names)

    # Collect line ranges of top-level nodes that match failing names
    include_ranges: List[tuple] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in failing_name_set:
                start = node.lineno - 1  # 0-indexed
                end = getattr(node, "end_lineno", node.lineno)  # 1-indexed end
                include_ranges.append((start, end))
            elif isinstance(node, ast.ClassDef):
                # Check if any method inside the class matches
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if child.name in failing_name_set:
                            start = node.lineno - 1
                            end = getattr(node, "end_lineno", node.lineno)
                            include_ranges.append((start, end))
                            break

    if not include_ranges:
        lines_out = lines[:_MAX_FIX_TEST_LINES]
        return "\n".join(lines_out) + "\n# ... (truncated)"

    # Find where the first class/function starts — everything before is imports/fixtures
    first_node_line = min(r[0] for r in include_ranges)
    # Collect imports: everything before the first top-level class/function
    preamble_end = 0
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            preamble_end = node.lineno - 1
            break

    result_lines = lines[:preamble_end]  # imports / module-level fixtures
    for start, end in sorted(set(include_ranges)):
        result_lines.append("")  # blank separator
        result_lines.extend(lines[start:end])

    focused = "\n".join(result_lines)

    # Safety cap — if somehow still large, truncate
    focused_lines = focused.splitlines()
    if len(focused_lines) > _MAX_FIX_TEST_LINES:
        focused = "\n".join(focused_lines[:_MAX_FIX_TEST_LINES]) + "\n# ... (truncated)"

    return focused


def _clean_llm_code_response(raw: str) -> str:
    """Clean an LLM response to extract only the Python code.

    Handles common LLM issues:
    - Reasoning text before/after code
    - Markdown code fences (```python ... ```)
    - XML tool calls (<function_calls>, <invoke>, etc.)
    - Mixed English + code output
    - "I need permission to write..." style refusals that wrap code in a block
    """
    if not raw:
        return ""
    code = raw.strip()

    # ── Step 1: If the whole response starts with a code fence, strip it ──
    if code.startswith("```"):
        lines = code.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        code = "\n".join(lines)

    # ── Step 2: Handle XML tool-call artifacts from agent mode ────────────
    _XML_AGENT_MARKERS = ("<function_calls>", "<invoke ", "</function_calls>", "<result>", "<invoke")
    if any(marker in code for marker in _XML_AGENT_MARKERS):
        import re as _re
        match = _re.search(r'```(?:python)?\s*\n(.*?)```', code, _re.DOTALL)
        if match:
            code = match.group(1).strip()
        else:
            return ""

    # ── Step 3: If response doesn't start with Python, look for embedded code ──
    # Handles cases like "I need permission..." or "Here is the fixed code:\n```python\n..."
    stripped = code.lstrip()
    if stripped and not stripped.startswith(_VALID_PYTHON_STARTS):
        import re as _re
        # First try: extract the largest ```python...``` block
        match = _re.search(r'```(?:python)?\s*\n(.*?)```', code, _re.DOTALL)
        if match:
            candidate = match.group(1).strip()
            if candidate and candidate.lstrip().startswith(_VALID_PYTHON_STARTS):
                return candidate

        # Second try: find the first line that looks like Python
        lines = code.split("\n")
        for idx, line in enumerate(lines):
            s = line.lstrip()
            if s.startswith(_VALID_PYTHON_STARTS):
                code = "\n".join(lines[idx:])
                break

    # ── Step 4: Strip any stray markdown fence lines that survived ──────────
    # Gemini sometimes appends ``` at the end after valid code, or mid-response.
    clean_lines = [l for l in code.split("\n") if l.strip() not in ("```", "```python", "```py")]
    code = "\n".join(clean_lines)

    return code.strip()


# Non-existent BaseConnector methods Gemini hallucinates — remove any lines referencing them
_HALLUCINATED_METHODS = frozenset([
    # save_config IS REAL — it lives in BaseConnector. Do NOT add it here.
    "_save_config", "save_token", "_save_token",
    "save_credentials", "_save_credentials", "save_state", "_save_state",
])


def _validate_file(path: Path) -> Dict[str, Any]:
    """Check if a Python file exists, is valid syntax, and isn't an error string."""
    if not path.exists():
        return {"valid": False, "reason": "missing"}
    content = path.read_text(encoding="utf-8")
    if len(content) < 50:
        return {"valid": False, "reason": "too_short", "content_preview": content[:100]}
    if not content.lstrip().startswith(_VALID_PYTHON_STARTS):
        return {"valid": False, "reason": "not_python", "content_preview": content[:100]}
    try:
        ast.parse(content)
    except SyntaxError as exc:
        return {"valid": False, "reason": "syntax_error", "error": str(exc)}
    return {"valid": True, "line_count": content.count("\n") + 1}


def _extract_connector_class_name(connector_path: Path) -> Optional[str]:
    """Parse connector.py and return the class name that extends BaseConnector.

    Falls back to finding any class whose name ends with 'Connector'.
    Returns None if no suitable class is found.
    """
    if not connector_path.exists():
        logger.warning("class_extract.missing", path=str(connector_path))
        return None
    try:
        tree = ast.parse(connector_path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        logger.warning("class_extract.syntax_error", path=str(connector_path), error=str(exc))
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            # Prefer class that inherits from BaseConnector
            for base in node.bases:
                base_name = ""
                if isinstance(base, ast.Name):
                    base_name = base.id
                elif isinstance(base, ast.Attribute):
                    base_name = base.attr
                if base_name == "BaseConnector":
                    return node.name

    # Fallback: any class ending with "Connector"
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.endswith("Connector"):
            return node.name

    return None


def _sync_init_with_connector(out_dir: Path, context: Dict[str, Any]) -> Optional[str]:
    """Read the actual class name from connector.py and rewrite __init__.py to match.

    Returns the actual class name, or None if extraction failed.
    """
    connector_path = out_dir / "connector.py"
    init_path = out_dir / "__init__.py"

    actual_class = _extract_connector_class_name(connector_path)
    if not actual_class:
        logger.warning("sync_init.no_class_found", connector=str(connector_path))
        return None

    # Rewrite __init__.py with the correct class name
    init_code = SCAFFOLD_INIT_TEMPLATE.format(
        service_name=context.get("service_name", ""),
        provider=context.get("provider", ""),
        auth_type=context.get("auth_type", ""),
        class_name=actual_class,
    )
    init_path.write_text(init_code, encoding="utf-8")
    return actual_class


def validate_step_output(step_type: str, out_dir: Path, result: Dict[str, Any]) -> Dict[str, Any]:
    """Validate that a step's expected outputs actually exist after the step completes.

    Returns {"valid": bool, "reason": str} — if valid is False, the step should
    NOT be marked completed in MongoDB even if the handler returned "pass".
    """
    def _file_ok(path: Path) -> bool:
        return path.exists() and path.stat().st_size > 10

    checks: Dict[str, bool] = {}

    if step_type in ("write_connector", "fix_connector", "syntax_check"):
        checks["connector.py"] = _file_ok(out_dir / "connector.py")

    if step_type == "configure_auth":
        checks["config.py"] = _file_ok(out_dir / "config.py")
        # connector.py does NOT exist yet at configure_auth stage — only config.py is created here

    if step_type == "scaffold_code":
        checks["__init__.py"] = _file_ok(out_dir / "__init__.py")
        checks["tests/__init__.py"] = _file_ok(out_dir / "tests" / "__init__.py")

    if step_type in ("write_tests", "run_tests"):
        checks["tests/test_connector.py"] = _file_ok(out_dir / "tests" / "test_connector.py")

    if step_type == "write_integration_tests":
        checks["tests/test_integration.py"] = _file_ok(out_dir / "tests" / "test_integration.py")

    if step_type == "run_integration_tests":
        # No file checks — UI-driven step, always valid
        return {"valid": True, "reason": "integration tests are UI-driven"}

    if step_type == "generate_test_guidelines":
        # Accept either local file or R2 success indicated in result output
        local_ok = _file_ok(out_dir / "test_guidelines.md")
        r2_ok = isinstance(result.get("output"), dict) and bool(result["output"].get("r2_key"))
        checks["test_guidelines.md"] = local_ok or r2_ok

    if step_type == "generate_metadata":
        checks["metadata/connector.json"] = _file_ok(out_dir / "metadata" / "connector.json")

    if step_type == "setup_instructions":
        checks["instructions/setup.md"] = _file_ok(out_dir / "instructions" / "setup.md")

    if step_type == "smoke_test":
        # Validated by output string — no new files produced
        output_str = str(result.get("output", ""))
        checks["smoke_passed"] = "SMOKE TEST PASSED" in output_str

    if step_type == "version_upgrade":
        meta_path = out_dir / "metadata" / "connector.json"
        if meta_path.exists():
            import json as _json
            try:
                meta = _json.loads(meta_path.read_text())
                checks["version_present"] = bool(meta.get("version"))
            except Exception:
                checks["version_present"] = False
        else:
            checks["version_present"] = False

    if step_type == "install_deps":
        # Pass-through — pip result already validated by handler
        checks["deps_ok"] = result.get("status") == "pass"

    # No file checks defined for this step type — trust handler result
    if not checks:
        return {"valid": True, "reason": "no file checks required"}

    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        return {"valid": False, "reason": f"Missing or empty output(s): {', '.join(failed)}"}
    return {"valid": True, "reason": "all outputs verified"}


def validate_generated_files(out_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Validate all generated files in the connector output directory.

    Returns a dict keyed by logical name (connector, config, init, tests)
    with validation result for each file.
    """
    return {
        "connector": _validate_file(out_dir / "connector.py"),
        "config": _validate_file(out_dir / "config.py"),
        "init": _validate_file(out_dir / "__init__.py"),
        "tests": _validate_file(out_dir / "tests" / "test_connector.py"),
    }


def _get_venv_python() -> str:
    """Return the shared venv Python executable (Python 3.13). Falls back to sys.executable."""
    try:
        from integration.services.shared_venv import get_venv_python
        return get_venv_python()
    except Exception:
        return sys.executable


def _pip_install_sync(pkg: str) -> Dict[str, Any]:
    """Run pip install synchronously — for asyncio.to_thread()."""
    try:
        python = _get_venv_python()
        proc = subprocess.run(
            [python, "-m", "pip", "install", "--quiet", pkg],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {"ok": proc.returncode == 0, "stderr": proc.stderr[:200]}
    except Exception as exc:
        return {"ok": False, "stderr": str(exc)}


async def _pip_install_async(pkg: str) -> Dict[str, Any]:
    """Cancellable async pip install. Kills the subprocess on CancelledError."""
    python = _get_venv_python()
    proc = subprocess.Popen(
        [python, "-m", "pip", "install", "--quiet", pkg],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def _wait() -> Dict[str, Any]:
        try:
            _, stderr = proc.communicate(timeout=120)
            return {"ok": proc.returncode == 0, "stderr": (stderr or "")[:200]}
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"ok": False, "stderr": f"Timeout installing {pkg}"}
        except Exception as exc:
            return {"ok": False, "stderr": str(exc)}

    try:
        return await asyncio.to_thread(_wait)
    except asyncio.CancelledError:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise


def _build_method_test_map(test_file: Path) -> Dict[str, List[str]]:
    """Parse a pytest test file and map connector method names to their test functions.

    Two strategies, in order:

    1. Class-based (preferred): classes named TestInstall, TestHealthCheck etc.
       TestInstall → install → ["test_install_success", "test_install_error"]

    2. Function-name-based (fallback): when all tests are in one generic class like
       TestGmailConnector. Groups test functions by their common name prefix:
       test_health_check_healthy, test_health_check_offline → health_check
       test_install → install

    Returns: {"install": ["test_install_success", ...], "health_check": [...], ...}
    """
    if not test_file.exists():
        return {}
    try:
        source = test_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception:
        return {}

    # ── Strategy 1: class-per-method ──────────────────────────────────────────
    result: Dict[str, List[str]] = {}
    all_funcs_in_file: List[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        cls_name = node.name
        if not cls_name.startswith("Test"):
            continue
        raw = cls_name[4:]  # strip "Test" prefix
        if not raw:
            continue
        # PascalCase → snake_case
        snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw).lower()
        test_funcs = [
            item.name
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name.startswith("test_")
        ]
        if test_funcs:
            all_funcs_in_file.extend(test_funcs)
            # Only add to result if class name looks like a specific method
            # (i.e. not a generic class like TestGmailConnector or TestConnector)
            # Heuristic: if snake contains "_connector" or "_client" it's generic
            if not snake.endswith("_connector") and not snake.endswith("_client") and snake not in ("connector", "client"):
                result[snake] = test_funcs

    # ── Strategy 2: function-name grouping (fallback for generic class) ───────
    # Collect ALL test functions not already assigned by strategy 1
    assigned_funcs: set = {f for funcs in result.values() for f in funcs}
    unassigned = [f for f in all_funcs_in_file if f not in assigned_funcs]

    if unassigned:
        used: set = set()
        for fn in unassigned:
            if fn in used or not fn.startswith("test_"):
                continue
            base = fn[5:]  # strip test_
            parts = base.split("_")

            # Find longest prefix shared by ≥2 functions (= the method name)
            method = base
            for n in range(1, len(parts)):
                cand = "_".join(parts[:n])
                siblings = [
                    f for f in unassigned
                    if f not in used and (
                        f.startswith(f"test_{cand}_") or f == f"test_{cand}"
                    )
                ]
                if len(siblings) >= 2:
                    method = cand  # keep extending as long as ≥2 share the prefix

            # Collect all functions for this method
            members = [
                f for f in unassigned
                if f not in used and (
                    f.startswith(f"test_{method}_") or f == f"test_{method}"
                )
            ]
            if not members:
                members = [fn]

            if method not in result:
                result[method] = []
            result[method].extend(members)
            used.update(members)

    return result


def _pytest_run_sync(tests_dir: Path, out_dir: Path, on_line=None) -> Dict[str, Any]:
    """Run pytest synchronously with streaming output — for asyncio.to_thread().

    Args:
        tests_dir: Path to tests directory
        out_dir: Output directory
        on_line: Optional callback(line: str) called for each output line to stream progress
    """
    try:
        # Resolve to absolute paths so pytest never misinterprets them
        # relative to its cwd.
        abs_tests_dir = tests_dir.resolve()
        abs_out_dir = out_dir.resolve()
        # PYTHONPATH needs:
        #  1. abs_out_dir          — so `from connector import XConnector` works
        #  2. abs_out_dir.parent   — fallback for package-style imports
        #  3. _SITE_PACKAGES       — installed packages (e.g. PyGithub's `github`) must come
        #                            BEFORE _CONNECTORS_ROOT so local dirs don't shadow them
        #  4. _VENV_SITE_PACKAGES  — shared Python 3.13 venv (pydantic, httpx, etc.)
        #  5. _CONNECTORS_ROOT     — so `from shared.base_connector import BaseConnector` works
        pythonpath = os.pathsep.join(filter(None, [
            str(abs_out_dir),
            str(abs_out_dir.parent),
            _VENV_SITE_PACKAGES,  # shared venv first (Python 3.13 deps)
            _SITE_PACKAGES,
            _USER_SITE,          # user pip packages (pip install --user)
            str(_CONNECTORS_ROOT),
        ]))

        # Use Popen for streaming output instead of run() which collects everything
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", str(abs_tests_dir), "-v", "--tb=short", "--no-header"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(abs_out_dir),
            env={**os.environ, "PYTHONPATH": pythonpath},
        )

        output_lines = []
        passed = 0
        failed = 0
        errors = 0

        # Stream output line-by-line while pytest runs
        try:
            for line in proc.stdout:
                line = line.rstrip('\n\r')
                output_lines.append(line)

                # Count results from this line
                if " PASSED" in line:
                    passed += 1
                if " FAILED" in line:
                    failed += 1
                if " ERROR" in line or line.startswith("ERROR "):
                    errors += 1

                # Emit progress to frontend if callback provided (keeps WebSocket alive)
                if on_line:
                    try:
                        on_line(line)
                    except Exception:
                        pass  # Don't break pytest if callback fails

            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"returncode": -1, "output": "Timeout after 120s", "passed": 0, "failed": 0, "errors": 0}

        output = "\n".join(output_lines)
        details = []
        for line in output_lines:
            if " PASSED" in line or " FAILED" in line or " SKIPPED" in line:
                parts = line.strip().split()
                if parts:
                    test_name = parts[0]
                    status = "passed" if " PASSED" in line else ("failed" if " FAILED" in line else "skipped")
                    details.append({"test": test_name, "status": status})
        return {
            "returncode": proc.returncode,
            "output": output,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "details": details,
        }
    except Exception as exc:
        return {"returncode": -1, "output": str(exc), "passed": 0, "failed": 0, "errors": 0}


async def _pytest_run_async(tests_dir: Path, out_dir: Path, on_line=None) -> Dict[str, Any]:
    """Cancellable async pytest runner.

    Unlike ``asyncio.to_thread(_pytest_run_sync, ...)``, this function creates
    the subprocess *before* entering the thread so that a ``CancelledError``
    (e.g. the user clicking Stop) can immediately ``proc.kill()`` the pytest
    process instead of waiting for it to finish naturally.
    """
    abs_tests_dir = tests_dir.resolve()
    abs_out_dir = out_dir.resolve()
    pythonpath = os.pathsep.join(filter(None, [
        str(abs_out_dir),
        str(abs_out_dir.parent),
        _VENV_SITE_PACKAGES,  # shared venv first (Python 3.13 deps)
        _SITE_PACKAGES,
        _USER_SITE,
        str(_CONNECTORS_ROOT),
    ]))

    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", str(abs_tests_dir), "-v", "--tb=short", "--no-header"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(abs_out_dir),
        env={**os.environ, "PYTHONPATH": pythonpath},
    )

    def _drain() -> tuple:
        """Read proc.stdout line-by-line (blocking) — runs in thread pool."""
        lines: list = []
        p = f = e = 0
        for line in proc.stdout:
            line = line.rstrip("\n\r")
            lines.append(line)
            if " PASSED" in line:
                p += 1
            if " FAILED" in line:
                f += 1
            if " ERROR" in line or line.startswith("ERROR "):
                e += 1
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
        return lines, p, f, e

    try:
        output_lines, passed, failed, errors = await asyncio.to_thread(_drain)
    except asyncio.CancelledError:
        # Stop button pressed — kill pytest immediately and propagate
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise

    output = "\n".join(output_lines)
    details = []
    for line in output_lines:
        if " PASSED" in line or " FAILED" in line or " SKIPPED" in line:
            parts = line.strip().split()
            if parts:
                test_name = parts[0]
                status = (
                    "passed" if " PASSED" in line
                    else ("failed" if " FAILED" in line else "skipped")
                )
                details.append({"test": test_name, "status": status})
    return {
        "returncode": proc.returncode,
        "output": output,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "details": details,
    }


# ── install_deps ─────────────────────────────────────────────────────

def _extract_packages_from_impl_plan(impl_plan_text: str) -> list[str]:
    """Extract package specifiers from the Dependencies section of implementation_plan.md.

    Matches any of these common LLM section header patterns:
      - ## 7. Dependencies
      - ## Package Dependencies
      - ## Required Packages
      - ## 7. Package Dependencies
    Also parses packages from code blocks inside the section (pip install commands,
    requirements.txt blocks), so version pins like google-auth==2.25.2 are preserved.

    Returns a deduplicated list of requirement specifiers (e.g. "google-auth==2.25.2").
    """
    import re as _re
    lines = impl_plan_text.splitlines()
    in_section = False
    in_code_block = False
    packages: list[str] = []

    # Match any Markdown heading that looks like a dependencies section
    _section_re = _re.compile(
        r"^#{1,3}\s*(?:\d+\.\s*)?"
        r"(?:package\s+dep|required\s+package|dependenc|pip\s+package|install\s+package)",
        _re.IGNORECASE,
    )
    _next_h2_re = _re.compile(r"^#{1,2}\s", _re.IGNORECASE)   # ## or # heading ends the section
    _code_fence_re = _re.compile(r"^```")

    # Matches bullet lines: - pkg or * pkg (with optional backticks/version)
    _bullet_re = _re.compile(r"^[-*]\s*`?([a-zA-Z][a-zA-Z0-9_\-]+(?:[>=!<,][^\s`#]+)?)`?")
    # Matches bare requirement lines inside code blocks: pkg>=x.y  or  pkg==x.y  or  pkg-name
    _req_re = _re.compile(r"^([a-zA-Z][a-zA-Z0-9_\-]+(?:[>=!<,][^\s#]+)?)(?:\s*#.*)?$")
    # Matches: pip install pkg==x.y  (captures full specifier)
    _pip_re = _re.compile(r"pip\s+install\s+([a-zA-Z][a-zA-Z0-9_\-]+(?:[>=!<,][^\s#]+)?)")
    # Skip prose lines
    _skip_re = _re.compile(r"^(The |This |For |Note|All |These |Each |File |import |from )", _re.IGNORECASE)

    for line in lines:
        stripped = line.strip()

        # Detect section start (any heading that is a dependency section)
        if _section_re.match(stripped):
            in_section = True
            in_code_block = False
            continue

        if not in_section:
            continue

        # Track code fences — must happen BEFORE the section-end check so that
        # comments like `# Core Gmail API SDK` inside a ```bash block are NOT
        # mistaken for a Markdown heading that would end the section.
        if _code_fence_re.match(stripped):
            in_code_block = not in_code_block
            continue

        # End section at the next same-level-or-higher heading (only outside code blocks)
        if not in_code_block and _next_h2_re.match(stripped):
            break

        if not stripped:
            continue

        pkg = None

        if in_code_block:
            # Inside a code block: parse pip install lines or bare requirement lines
            m_pip = _pip_re.search(stripped)
            if m_pip:
                pkg = m_pip.group(1).strip()
            elif not stripped.startswith("#"):
                m_req = _req_re.match(stripped)
                if m_req:
                    pkg = m_req.group(1).strip()
        else:
            # Outside a code block: parse bullet lists and pip lines
            m_pip = _pip_re.search(stripped)
            if m_pip:
                pkg = m_pip.group(1).strip()
            else:
                m_b = _bullet_re.match(stripped)
                if m_b:
                    pkg = m_b.group(1).strip()

        if pkg and len(pkg) > 2 and not pkg.startswith("-"):
            packages.append(pkg)

    # Deduplicate by normalised package name (ignore version specifier for dedup key)
    _name_only = _re.compile(r"^([a-zA-Z][a-zA-Z0-9_\-]+)")
    seen: set[str] = set()
    result: list[str] = []
    for p in packages:
        m = _name_only.match(p)
        key = m.group(1).lower().replace("-", "_") if m else p.lower()
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def _is_common_dep(pkg_spec: str) -> bool:
    """Return True if the package is already pre-installed in the shared venv (COMMON_DEPS).

    Prevents downgrading pre-installed packages like pydantic/httpx/structlog by
    skipping them when the implementation plan or requirements.txt pins old versions.
    """
    import re as _re
    try:
        from integration.services.shared_venv import COMMON_DEPS as _cd
        _name_re = _re.compile(r"^([a-zA-Z][a-zA-Z0-9_\-]+)")
        pkg_name = (_name_re.match(pkg_spec.strip()) or _name_re.match("x")).group(1).lower().replace("-", "_")
        common_names = set()
        for dep in _cd:
            m = _name_re.match(dep)
            if m:
                common_names.add(m.group(1).lower().replace("-", "_"))
        return pkg_name in common_names
    except Exception:
        return False


async def handle_install_deps(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Install Python packages using the shared venv (Python 3.13).

    Package source priority:
      1. implementation_plan.md Dependencies section (most accurate)
      2. config.packages from the plan step (planner LLM fallback)
      3. requirements.txt in the connector output directory (last resort)

    Packages already pre-installed in the shared venv (pydantic, httpx, structlog,
    google-auth libs, pytest plugins) are skipped to prevent accidental downgrades.
    The shared venv Python is always used — never the system Python.
    """
    import re as _re
    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    impl_plan_path = out_dir / "implementation_plan.md"

    # ── Priority 1: implementation_plan.md Dependencies section ──────────────
    plan_packages: list[str] = []
    if impl_plan_path.exists():
        try:
            impl_text = impl_plan_path.read_text(encoding="utf-8")
            plan_packages = _extract_packages_from_impl_plan(impl_text)
            if plan_packages:
                await _emit(log_cb, "info", f"📦 Using packages from implementation_plan.md: {', '.join(plan_packages)}")
        except Exception as _e:
            await _emit(log_cb, "warn", f"Could not parse implementation_plan.md packages: {_e}")

    # ── Priority 2: config.packages ───────────────────────────────────────────
    packages = plan_packages or config.get("packages", [])

    # ── Priority 3: requirements.txt ─────────────────────────────────────────
    if not packages:
        req_txt = out_dir / "requirements.txt"
        if req_txt.exists():
            try:
                req_lines = req_txt.read_text(encoding="utf-8").splitlines()
                _pkg_re = _re.compile(r"^([a-zA-Z][a-zA-Z0-9_\-]+(?:[>=!<,][^\s#]+)?)")
                for ln in req_lines:
                    ln = ln.strip()
                    if not ln or ln.startswith("#") or ln.startswith("-"):
                        continue
                    m = _pkg_re.match(ln)
                    if m:
                        packages.append(m.group(1).strip())
                if packages:
                    await _emit(log_cb, "info", f"📦 Using packages from requirements.txt: {', '.join(packages)}")
            except Exception as _e:
                await _emit(log_cb, "warn", f"Could not parse requirements.txt: {_e}")

    if not packages:
        await _emit(log_cb, "warn", "No packages found — skipping install")
        return {"status": "pass", "output": "No packages to install"}

    # ── Filter out packages already pre-installed in the shared venv ──────────
    # This prevents accidentally downgrading pydantic/httpx/structlog to old pinned
    # versions that lack Python 3.13/3.14 pre-built wheels (e.g. pydantic==2.5.3
    # requires pydantic-core 2.14.6 which has no Python 3.14 wheel → build failure).
    to_install = [p for p in packages if not _is_common_dep(p)]
    skipped    = [p for p in packages if     _is_common_dep(p)]
    if skipped:
        await _emit(log_cb, "info", f"⏭  Skipping pre-installed common deps: {', '.join(skipped)}")
    if not to_install:
        await _emit(log_cb, "info", "✓ All packages are pre-installed in shared venv — nothing to install")
        return {"status": "pass", "output": {"installed": [], "failed": [], "skipped": skipped}}

    await _emit(log_cb, "info", f"Installing {len(to_install)} package(s) via shared venv (Python 3.13): {', '.join(to_install)}")

    installed = []
    failed = []
    for pkg in to_install:
        result = await _pip_install_async(pkg)
        if result["ok"]:
            installed.append(pkg)
            await _emit(log_cb, "success", f"  ✓ {pkg}")
        else:
            failed.append(pkg)
            await _emit(log_cb, "error", f"  ✗ {pkg}: {result['stderr']}")

    status = "pass" if not failed else ("partial" if installed else "fail")
    return {
        "status": status,
        "output": {"installed": installed, "failed": failed, "skipped": skipped},
    }


# ── configure_auth ───────────────────────────────────────────────────

async def handle_configure_auth(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Generate auth configuration boilerplate."""
    provider = context.get("provider", "unknown")
    service_name = context.get("service_name", "Unknown Service")
    auth_type = config.get("auth_type", context.get("auth_type", "oauth2"))
    scopes = config.get("scopes", context.get("default_scopes", []))
    out_dir = _output_dir(context["tenant_id"], context["service_slug"])

    await _emit(log_cb, "info", f"Generating auth config for {auth_type}")

    # Use template for config — pick OAuth vs non-OAuth based on auth_type
    class_name = context.get("class_name", "Connector")
    env_prefix = provider.upper()
    api_base_url = context.get("docs_url", f"https://api.{provider}.com")

    _oauth_types = {"oauth2", "oauth2_code", "oauth2_pkce", "oauth2_client_credentials"}
    _config_template = SCAFFOLD_CONFIG_TEMPLATE_OAUTH if auth_type in _oauth_types else SCAFFOLD_CONFIG_TEMPLATE_APIKEY

    config_code = _config_template.format(
        service_name=service_name,
        class_name=class_name,
        provider=provider,
        default_scopes=json.dumps(scopes),
        api_base_url=api_base_url,
        env_prefix=env_prefix,
    )

    config_path = out_dir / "config.py"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(config_code, encoding="utf-8")

    await _emit(log_cb, "success", f"Auth config written → {config_path.name}")
    return {
        "status": "pass",
        "output": {"file": str(config_path), "auth_type": auth_type},
    }


# ── scaffold_code ────────────────────────────────────────────────────

async def handle_scaffold_code(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Create directory structure and __init__.py."""
    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    out_dir.mkdir(parents=True, exist_ok=True)

    await _emit(log_cb, "info", f"Scaffolding directory: {out_dir}")

    # Create __init__.py
    class_name = context.get("class_name", "Connector")
    init_code = SCAFFOLD_INIT_TEMPLATE.format(
        service_name=context.get("service_name", ""),
        provider=context.get("provider", ""),
        auth_type=context.get("auth_type", ""),
        class_name=class_name,
    )
    (out_dir / "__init__.py").write_text(init_code, encoding="utf-8")

    # Create tests directory
    tests_dir = out_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "__init__.py").write_text("# Tests package\n", encoding="utf-8")

    created = ["__init__.py", "tests/__init__.py"]
    for f in created:
        await _emit(log_cb, "success", f"  ✓ {f}")

    # Create subdirectories: helpers/, client/
    for subdir in ["helpers", "client"]:
        sub = out_dir / subdir
        sub.mkdir(exist_ok=True)
        (sub / "__init__.py").write_text(f'"""{subdir.replace("_", " ").title()} package."""\n', encoding="utf-8")
        created.append(f"{subdir}/__init__.py")
        await _emit(log_cb, "success", f"  ✓ {subdir}/__init__.py")

    # Initialise ImplementationTimeline.md for this session (idempotent — appends new session)
    _append_timeline(out_dir, context.get("session_id", ""), "Scaffold Package Structure", 0, "pass", 0)

    return {
        "status": "pass",
        "output": {"directory": str(out_dir), "files_created": created},
    }


# ── write_connector ──────────────────────────────────────────────────


async def handle_write_connector(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Use LLM to generate the main connector.py file."""
    provider = context.get("provider", "unknown")
    service_name = context.get("service_name", "Unknown Service")
    out_dir = _output_dir(context["tenant_id"], context["service_slug"])

    await _emit(log_cb, "info", f"Generating connector code for {service_name} via {_llm_label()}...")

    # Build a structured constraints block from everything the planner extracted into the
    # write_connector step's config.  This bridges planning → codegen: the LLM only sees
    # the prompt template, not the original user prompt a second time, so every constraint
    # the planner identified must be surfaced here explicitly.
    # NOTE: built BEFORE the Gemini block so plan_constraints is always defined.
    plan_constraints_lines: list[str] = []
    if config.get("methods"):
        plan_constraints_lines.append(f"- **Methods to implement**: {', '.join(config['methods'])}")
    if config.get("architecture_notes"):
        for note in config["architecture_notes"]:
            plan_constraints_lines.append(f"- **Architecture**: {note}")
    if config.get("env_vars"):
        plan_constraints_lines.append(
            f"- **Environment variables** (use these EXACT names, not alternatives): {', '.join(config['env_vars'])}"
        )
    if config.get("response_format"):
        plan_constraints_lines.append(f"- **Response format**: {config['response_format']}")
    if config.get("error_patterns"):
        for ep in config["error_patterns"]:
            plan_constraints_lines.append(f"- **Error handling**: {ep}")
    if config.get("iam_notes"):
        for note in config["iam_notes"]:
            plan_constraints_lines.append(f"- **IAM/Permissions**: {note}")
    if config.get("features"):
        plan_constraints_lines.append(f"- **Required features**: {', '.join(config['features'])}")

        # ── Handler feature implementation directions ──────────────────────
        # When handler features are selected, inject specific directions so the
        # LLM knows these methods exist on BaseConnector and must be overridden
        # with the correct signatures — not invented from scratch.
        _handler_features = [f for f in config["features"] if f in (
            "handle_webhook", "process_callback", "handle_event", "batch_processor"
        )]
        if _handler_features:
            plan_constraints_lines.append(
                "- **Handler methods** — these are BaseConnector lifecycle methods. "
                "OVERRIDE them (do NOT create new method names). Signatures:"
            )
            _handler_sigs = {
                "handle_webhook": (
                    "  - `async def handle_webhook(self, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]` "
                    "— Route events by type using private `_handle_<event_type>()` methods. "
                    "Return `{\"status\": \"processed\"|\"ignored\"}`. "
                    "Call `self.process_callback(payload, headers)` first if signature verification is needed. "
                    "Add `webhook_secret` to install_fields if the provider signs payloads."
                ),
                "process_callback": (
                    "  - `async def process_callback(self, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]` "
                    "— Verify webhook signature (HMAC-SHA256 or provider-specific). "
                    "Use `hmac.compare_digest()` for timing-safe comparison. "
                    "Read secret from `self.config.get(\"webhook_secret\")`. "
                    "Return `{\"verified\": True, \"data\": payload}` or `{\"verified\": False, \"error\": \"reason\"}`."
                ),
                "handle_event": (
                    "  - `async def handle_event(self, event: Dict[str, Any]) -> Dict[str, Any]` "
                    "— Process a single event. Check idempotency (skip duplicate event IDs). "
                    "Return `{\"event_id\": ..., \"processed\": True}`."
                ),
                "batch_processor": (
                    "  - `async def batch_processor(self, items: list, **kwargs) -> Dict[str, Any]` "
                    "— Process items one by one. Catch per-item errors — never fail the whole batch. "
                    "Return `{\"processed\": N, \"failed\": N, \"errors\": [...]}`."
                ),
            }
            for hf in _handler_features:
                plan_constraints_lines.append(_handler_sigs[hf])

    plan_constraints = (
        "\n".join(plan_constraints_lines)
        if plan_constraints_lines
        else "(No additional constraints extracted from plan — follow the user requirements above.)"
    )

    # ── Auto-scaffold package structure before Gemini runs ───────────────────
    # scaffold_code and configure_auth are no longer separate plan steps, so
    # write_connector is responsible for ensuring the standard layout exists.
    # Gemini's system prompt says these are "already there" — make that true.
    _auth_type = context.get("auth_type", "api_key")
    _class_name = context.get("class_name", "Connector")
    _service_name = context.get("service_name", "")
    _provider = context.get("provider", "")
    _env_prefix = context.get("service_slug", _service_name.lower().replace(" ", "_")).upper()

    # __init__.py — only create if absent (cleanup may have wiped it)
    if not (out_dir / "__init__.py").exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        _init_code = SCAFFOLD_INIT_TEMPLATE.format(
            service_name=_service_name,
            provider=_provider,
            auth_type=_auth_type,
            class_name=_class_name,
        )
        (out_dir / "__init__.py").write_text(_init_code, encoding="utf-8")
        await _emit(log_cb, "success", "  ✓ __init__.py scaffolded")

    # config.py — create with appropriate auth template if missing
    if not (out_dir / "config.py").exists():
        _oauth_types = {"oauth2_code", "oauth2_pkce", "oauth2_client_credentials", "oauth2"}
        _cfg_template = SCAFFOLD_CONFIG_TEMPLATE_OAUTH if _auth_type in _oauth_types else SCAFFOLD_CONFIG_TEMPLATE_APIKEY
        _cfg_code = _cfg_template.format(
            service_name=_service_name,
            class_name=_class_name,
            env_prefix=_env_prefix,
        )
        (out_dir / "config.py").write_text(_cfg_code, encoding="utf-8")
        await _emit(log_cb, "success", "  ✓ config.py scaffolded")

    # tests/, helpers/, client/ — create dirs + __init__.py if missing
    for _subdir, _comment in [
        ("tests",   "# Tests package\n"),
        ("helpers", '"""Helpers package."""\n'),
        ("client",  '"""Client package."""\n'),
    ]:
        _sub = out_dir / _subdir
        _sub.mkdir(exist_ok=True)
        _init = _sub / "__init__.py"
        if not _init.exists():
            _init.write_text(_comment, encoding="utf-8")
            await _emit(log_cb, "success", f"  ✓ {_subdir}/__init__.py scaffolded")

    # ── Connector generation: Gemini agentic OR Claude CLI ───────────────────
    if False:  # gemini path disabled — Claude is the only backend codegen runtime
        # Gemini agentic path (reads base_connector.py, writes + validates in a tool-call loop)
        try:
            from integration.services.agentic_fix import gemini_agentic_generate_connector
            result = await gemini_agentic_generate_connector(
                out_dir,
                context={**context, "plan_constraints": plan_constraints},
                log_cb=log_cb,
            )
            if not result["success"] or not (out_dir / "connector.py").exists():
                _fail_reason = result.get("message") or result.get("result") or "Agentic loop did not complete successfully"
                await _emit(log_cb, "error", f"Connector generation failed: {_fail_reason}")
                return {"status": "fail", "output": _fail_reason}

            connector_path = out_dir / "connector.py"
            code = connector_path.read_text(encoding="utf-8")
            await _emit(log_cb, "success", f"✅ Connector generated ({len(code.splitlines())} lines) in {result['iterations']} iteration(s)")
            _sync_init_with_connector(out_dir, context)

        except Exception as _agentic_err:
            _emsg = str(_agentic_err)
            await _emit(log_cb, "error", f"Connector generation failed: {_emsg[:200]}")
            return {"status": "fail", "output": _emsg}

    else:
        # Claude CLI path — direct single-shot generation
        await _emit(log_cb, "info", f"Generating connector via {_llm_label()}...")
        try:
            _conn_system = (await _get_prompt("CONNECTOR_SYSTEM_PROMPT", CONNECTOR_SYSTEM_PROMPT)).format(
                base_connector_interface=BASE_CONNECTOR_INTERFACE,
                provider=_get_ctx(context, "provider", "unknown"),
                service_name=_get_ctx(context, "service_name", "Unknown Service"),
                connector_name=_get_ctx(context, "connector_name", _get_ctx(context, "service_name")),
                auth_type=_get_ctx(context, "auth_type", "api_key"),
                sdk_package=_get_ctx(context, "sdk_package", "httpx"),
                docs_url=_get_ctx(context, "docs_url", ""),
                default_scopes=_get_ctx(context, "default_scopes", ""),
                user_prompt=_get_ctx(context, "user_prompt", "(not provided)"),
                plan_constraints=plan_constraints,
                step_memory_summary=_build_step_memory_summary(context),
            )
            _conn_system = await _inject_rag_context(_conn_system, context)

            code = await call_llm_fix(
                [{"role": "user", "content": "Generate the connector.py for this service. Return ONLY raw Python code — no markdown fences, no prose."}],
                system=_conn_system,
                max_tokens=60000,
            )
            code = _clean_llm_code_response(code)

            if not code or len(code) < 100 or not code.lstrip().startswith(_VALID_PYTHON_STARTS):
                await _emit(log_cb, "error", f"Claude returned invalid connector code: {code[:120]}")
                return {"status": "fail", "output": f"LLM did not return valid Python: {code[:200]}"}

            try:
                ast.parse(code)
            except SyntaxError as _syn:
                await _emit(log_cb, "warn", f"Syntax error at line {_syn.lineno} — {_syn}")
                return {"status": "fail", "output": f"Generated connector has syntax error: {_syn}"}

            connector_path = out_dir / "connector.py"
            connector_path.write_text(code, encoding="utf-8")
            await _emit(log_cb, "success", f"✅ Connector generated ({len(code.splitlines())} lines)")
            _sync_init_with_connector(out_dir, context)

        except Exception as _claude_err:
            _emsg = str(_claude_err)
            await _emit(log_cb, "error", f"Connector generation failed: {_emsg[:200]}")
            return {"status": "fail", "output": _emsg}

    # ── Re-read connector.py from disk to get the final version Gemini produced ──
    # (Gemini may have self-corrected the file after its first write)
    connector_path = out_dir / "connector.py"
    code = connector_path.read_text(encoding="utf-8")
    quality = analyze_file(str(connector_path))

    # ── Enumerate ALL files Gemini wrote (client/, helpers/, exceptions.py, etc.) ──
    # Gemini is responsible for writing the full package — no second-pass module generation.
    all_pkg_files = [
        str(f.relative_to(out_dir))
        for f in sorted(out_dir.rglob("*"))
        if f.is_file() and "__pycache__" not in str(f) and not str(f.relative_to(out_dir)).startswith("tests/")
    ]
    await _emit(log_cb, "success",
        f"✅ Package written — {len(all_pkg_files)} file(s): {', '.join(all_pkg_files)}")

    # ── Upload all generated files to R2 (production source of truth) ──────────
    try:
        count = await r2_service.upload_connector_dir(
            context["tenant_id"], context["service_slug"], context["session_id"], out_dir
        )
        r2_prefix = r2_service.connector_session_r2_prefix(
            context["tenant_id"], context["service_slug"], context["session_id"]
        )
        await _emit(log_cb, "success", f"  ↑ {count} file(s) synced to R2 [{r2_prefix}]")
    except Exception as _r2_err:
        await _emit(log_cb, "warn", f"  R2 upload skipped: {_r2_err}")
        r2_prefix = ""

    return {
        "status": "pass",
        "output": {
            "file": str(connector_path),
            "line_count": quality.get("line_count", 0),
            "quality_score": quality.get("quality_score", 0),
            "files_generated": all_pkg_files,
            "r2_prefix": r2_prefix,
        },
    }


# ── write_tests ──────────────────────────────────────────────────────

async def handle_write_tests(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Use LLM to generate test files."""
    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    connector_path = out_dir / "connector.py"

    if not connector_path.exists():
        await _emit(log_cb, "error", "connector.py not found — run write_connector first")
        return {"status": "fail", "output": "connector.py missing"}

    # ── Load connector-specific test guidelines from R2 / local file ─────────
    guidelines_content = ""
    guidelines_local = out_dir / "test_guidelines.md"
    if guidelines_local.exists():
        guidelines_content = guidelines_local.read_text(encoding="utf-8")
        await _emit(log_cb, "info", "Loaded connector-specific test guidelines from local file")
    else:
        try:
            guidelines_content = await r2_service.get_test_guidelines(
                context.get("provider", ""), context.get("service_slug", "")
            ) or ""
            if guidelines_content:
                await _emit(log_cb, "info", "Loaded connector-specific test guidelines from R2")
        except Exception:
            pass

    connector_code = connector_path.read_text(encoding="utf-8")
    # When called from handle_fix_tests (regeneration path), skip extra module test files.
    is_regen = context.get("_regen_tests_only", False)
    await _emit(log_cb, "info", f"Regenerating tests via {_llm_label()}..." if is_regen else f"Generating tests via {_llm_label()}...")

    # ── Always extract class_name from the actual connector.py (session context may be stale) ──
    _class_match = re.search(r"^class\s+(\w+)\s*\(BaseConnector\)", connector_code, re.MULTILINE)
    class_name = _class_match.group(1) if _class_match else context.get("class_name", "Connector")

    # ── Generate service-specific test_rules.md via Claude CLI ───────────
    await _emit(log_cb, "info", f"  Connector class detected: {class_name}")

    # ── Load TEST_CASE_WRITING_GUIDELINES.md and prepend to system prompt ──
    # R2 shared bucket → local disk fallback (via guidelines_service)
    from integration.services.guidelines_service import get_test_case_writing_guidelines
    _guidelines_content = await get_test_case_writing_guidelines()
    _guidelines_header = ""
    if _guidelines_content:
        _guidelines_header = (
            "## ══════════════════════════════════════════════════════════\n"
            "## SHIELVA TEST CASE WRITING GUIDELINES — READ BEFORE CODING\n"
            "## ══════════════════════════════════════════════════════════\n"
            + _guidelines_content
            + "\n## ══════════════ END OF GUIDELINES ══════════════\n\n"
        )
        await _emit(log_cb, "info", "📋 TEST_CASE_WRITING_GUIDELINES.md loaded into prompt")
    else:
        await _emit(log_cb, "warn", "⚠ TEST_CASE_WRITING_GUIDELINES.md not found in R2 or local disk — using inline rules only")


    system = (await _get_prompt("TEST_SYSTEM_PROMPT", TEST_SYSTEM_PROMPT)).format(
        connector_code=connector_code,
        provider=_get_ctx(context, "provider", "unknown"),
        service_name=_get_ctx(context, "service_name", "Unknown Service"),
        connector_name=_get_ctx(context, "connector_name", _get_ctx(context, "service_name")),
        auth_type=_get_ctx(context, "auth_type", "unknown"),
        sdk_package=_get_ctx(context, "sdk_package"),
        user_prompt=_get_ctx(context, "user_prompt", "(not provided)"),
        step_memory_summary=_build_step_memory_summary(context),
        class_name=class_name,
    )
    # Inject RAG knowledge context
    system = await _inject_rag_context(system, context)
    # Prepend the full guidelines so they appear first — LLMs pay most attention to the top.
    # Then add the mandatory import line as a hard reinforcement.
    _pkg_name = out_dir.name  # e.g. paytm_upi_connector
    mandatory_import_block = (
        f"## ⚠️ MANDATORY IMPORT BLOCK — COPY THIS EXACT PATTERN (do NOT omit the 'from connector import' line):\n"
        f"## The test runner sets cwd={_pkg_name}/ so bare 'connector' module IS connector.py in that directory.\n"
        f"## NEVER use 'from {_pkg_name}.connector import' — it creates a SECOND module instance\n"
        f"## and breaks all isinstance/exception checks.\n"
        f"## @patch paths must use 'connector.X' (bare), NOT '{_pkg_name}.connector.X'.\n"
        f"##\n"
        f"## CORRECT PATTERN:\n"
        f"##   from connector import (\n"
        f"##       {class_name},\n"
        f"##       PaytmAPIError,\n"
        f"##       # ... other names ...\n"
        f"##   )\n"
        f"## WRONG (do NOT use): from {_pkg_name}.connector import {class_name}\n\n"
    )
    # Patch guidelines in memory before injecting — fixes stale/hallucinated class names,
    # exception names, and config keys without requiring a re-generation step.
    if guidelines_content:
        try:
            guidelines_content = await _validate_and_patch_guidelines(guidelines_content, out_dir, log_cb=None)
        except Exception:
            pass
    # Inject connector-specific test guidelines (from generate_test_guidelines step) if available
    _connector_guidelines_block = ""
    if guidelines_content:
        _connector_guidelines_block = (
            "## ══════════════════════════════════════════════════════════\n"
            "## CONNECTOR-SPECIFIC TEST GUIDELINES (auto-generated)\n"
            "## ══════════════════════════════════════════════════════════\n"
            + guidelines_content
            + "\n## ══════════════ END OF CONNECTOR GUIDELINES ══════════════\n\n"
        )

    # Inject ground truth from connector AST into prompt (Layer 1 — prevents hallucination)
    _write_tests_ground_truth = _extract_connector_ground_truth(out_dir)
    _ground_truth_block_write = ""
    if _write_tests_ground_truth:
        _ground_truth_block_write = (
            "## ══════════════════════════════════════════════════════════\n"
            "## GROUND TRUTH — EXACT NAMES FROM CONNECTOR SOURCE (DO NOT DEVIATE)\n"
            "## ══════════════════════════════════════════════════════════\n"
            + _write_tests_ground_truth
            + "\n## ══════════════ END OF GROUND TRUTH ══════════════\n\n"
        )
        await _emit(log_cb, "info", "Ground truth (AST-extracted) injected into test generation prompt")

    system = _ground_truth_block_write + _guidelines_header + _connector_guidelines_block + mandatory_import_block + system

    # Inject session plan steps so Gemini knows what each method is supposed to do.
    # Without the plan, Gemini can only guess behavior from the connector code (circular).
    _plan_steps = context.get("plan", {}).get("steps", []) if isinstance(context.get("plan"), dict) else []
    if _plan_steps:
        _plan_lines = []
        for _i, _s in enumerate(_plan_steps):
            _stype = _s.get("type", "") if isinstance(_s, dict) else ""
            _desc = (_s.get("description", "") or _s.get("name", "")) if isinstance(_s, dict) else ""
            if _stype or _desc:
                _plan_lines.append(f"  Step {_i + 1} ({_stype}): {_desc}")
        if _plan_lines:
            system += (
                "\n\n## CONNECTOR BUILD PLAN (the specification — what each method must implement)\n"
                + "\n".join(_plan_lines)
            )
            await _emit(log_cb, "info", f"📋 Plan steps ({len(_plan_lines)}) injected into test generation prompt")

    # Inject prior failure history so Gemini writes tests that avoid known issues.
    # error_details is pre-loaded by execute_plan() in codegen_service.py before calling this handler.
    failure_ctx = context.get("error_details")
    if failure_ctx:
        system += f"\n\n## ⚠ Known test failures from previous runs — write tests that avoid these patterns\n{failure_ctx}"
        await _emit(log_cb, "info", "📋 Prior failure history injected into test generation prompt")

    # ── Gemini agentic path — tool-calling loop (read files → write tests → run → fix) ──
    if False:  # gemini path disabled — Claude is the only backend codegen runtime
        try:
            from integration.services.agentic_fix import _gemini_agentic_loop, _FIX_TOOLS, _enhance_directive
            await _emit(log_cb, "info", "🤖 Gemini agentic write_tests (tool-call loop: read → write → run → fix)...")
            _tests_initial = (
                f"Generate pytest unit tests for the connector in: {out_dir.name}/\n\n"
                f"Connector class: {class_name}\n\n"
                "Steps:\n"
                "1. read_file('connector.py') — exact class name, method signatures, client attribute name, every awaited call\n"
                "2. Read client/*.py files — identify which methods are async def (MUST mock those as AsyncMock)\n"
                "3. Read helpers/*.py if connector imports from helpers\n"
                "4. Write tests/test_connector.py — follow ALL guidelines in the system prompt exactly\n"
                "5. validate_python('tests/test_connector.py') — fix any syntax errors\n"
                "6. run_tests — analyse failures and fix root causes; iterate until ALL pass\n"
                "7. done(summary) when all tests pass\n\n"
                "CRITICAL: mock set_token, get_token, ingest_batch in every test calling install/authorize/sync.\n"
                "CRITICAL: write to tests/test_connector.py — not to the package root."
            )
            if context.get("is_enhance"):
                _tests_initial += _enhance_directive(out_dir, artifact="tests",
                                                    enhancement_ask=context.get("user_prompt", ""))
            agentic_result = await _gemini_agentic_loop(
                out_dir,
                system_prompt=system,   # pre-built rich system with ground truth + guidelines
                initial_message=_tests_initial,
                tools=_FIX_TOOLS,
                log_cb=log_cb,
                max_iterations=20,
                stop_on_done=True,
                stop_on_tests_pass=True,
                protected_files={"connector.py"},  # never overwrite connector during test gen
            )
            if agentic_result.get("success") and (out_dir / "tests" / "test_connector.py").exists():
                pytest_out = await _pytest_run_async(out_dir / "tests", out_dir)
                passed = pytest_out.get("passed", 0)
                failed = pytest_out.get("failed", 0)
                await _emit(log_cb, "success" if failed == 0 else "warn",
                    f"✅ Agentic write_tests done in {agentic_result['iterations']} iteration(s) — {passed} passed, {failed} failed")
                return {
                    "status": "pass" if failed == 0 else "fail",
                    "output": {
                        "passed": passed, "failed": failed,
                        "test_file": str(out_dir / "tests" / "test_connector.py"),
                    },
                }
            else:
                await _emit(log_cb, "warn", "Gemini agentic write_tests incomplete — falling back to direct generation")
        except Exception as _ag_err:
            await _emit(log_cb, "warn", f"Gemini agentic write_tests failed ({_ag_err}) — falling back to direct generation")

    messages = [
        {"role": "user", "content": "Output comprehensive pytest test code for this connector. Return ONLY raw Python — no prose, no tools, no file operations."},
    ]

    try:
        code = await call_llm_fix(messages, system=system, max_tokens=60000,
                                   on_chunk=_make_gemini_progress_cb(log_cb, "Gemini tests"))
        code = _clean_llm_code_response(code)

        # Guard: reject obviously non-Python LLM responses
        if not code or len(code) < 50 or (not code.lstrip().startswith(_VALID_PYTHON_STARTS)):
            await _emit(log_cb, "error", f"LLM returned invalid response (not Python code): {code[:120]}")
            return {"status": "fail", "output": f"LLM did not return valid Python test code: {code[:200]}"}

        # Validate syntax — if broken (likely truncated), ask Gemini to continue from last clean line
        try:
            ast.parse(code)
        except SyntaxError as exc:
            lines = code.splitlines()
            # Keep only lines before the error — send that as context and ask Gemini to continue
            clean_lines = lines[:max(0, (exc.lineno or len(lines)) - 1)]
            clean_prefix = "\n".join(clean_lines)
            await _emit(log_cb, "warn", f"Test code truncated at line {exc.lineno} — asking Gemini to continue from line {len(clean_lines)}...")
            continuation_messages = [
                {
                    "role": "user",
                    "content": (
                        f"A Python test file was truncated at line {exc.lineno}. "
                        f"Here is everything up to the truncation point:\n\n"
                        f"```python\n{clean_prefix}\n```\n\n"
                        f"Continue writing the rest of the file from exactly where it was cut off. "
                        f"Close all open classes and functions. "
                        f"Output ONLY the continuation (do NOT repeat what was already written). "
                        f"Start from the next line after line {len(clean_lines)}."
                    ),
                },
            ]
            continuation_system = (
                "Output only Python code — the continuation of a truncated file. "
                "No prose. No markdown. Do NOT repeat existing lines. "
                "Close all open indentation blocks and functions properly."
            )
            continuation = await call_llm_fix(continuation_messages, system=continuation_system, max_tokens=60000,
                                              on_chunk=_make_gemini_progress_cb(log_cb, "Gemini continuation"))
            continuation = _clean_llm_code_response(continuation)
            code = clean_prefix + "\n" + continuation
            try:
                ast.parse(code)
                await _emit(log_cb, "success", f"✅ Test file completed ({len(code.splitlines())} lines total)")
            except SyntaxError as exc2:
                await _emit(log_cb, "error", f"Still broken after continuation: {exc2} — aborting")
                return {"status": "fail", "output": f"Generated test code has persistent syntax error: {exc2}"}

        # ── Post-process: fix connector import path ────────────────────
        # Gemini frequently hallucinates the package name (e.g. google_adsense_connector,
        # adsense_connector.connector, client.connector).  The only correct import is
        # `from connector import <ClassName>` because out_dir is on PYTHONPATH and
        # connector.py lives at the package root.  Fix this deterministically so tests
        # never fail due to a wrong module path.
        code = _fix_connector_import(code, class_name)
        # Strip hallucinated import names that don't actually exist in connector.py
        code = _strip_hallucinated_imports(code, out_dir / "connector.py")
        await _emit(log_cb, "info", f"✔ Import paths normalised → from connector import {class_name}")

        tests_dir = out_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        test_path = tests_dir / "test_connector.py"
        test_path.write_text(code, encoding="utf-8")

        # ── Non-AI syntax auto-fix (autoflake → ruff → ast.parse) ──────────
        await _emit(log_cb, "info", "🔧 Running syntax auto-fix (autoflake → ruff)...")
        from integration.services.code_quality import auto_fix_python_file
        _fix = auto_fix_python_file(test_path)
        if _fix["tools_applied"]:
            await _emit(log_cb, "info", f"✔ Auto-fixed with: {', '.join(_fix['tools_applied'])}")
        if _fix["clean"]:
            await _emit(log_cb, "success", "✅ Syntax check passed after auto-fix")
        else:
            await _emit(log_cb, "warn", f"⚠ Syntax issue remains after auto-fix: {_fix.get('syntax_error', 'unknown')} — will be resolved by Attempt Fix")
        # Re-read in case tools modified the file
        code = test_path.read_text(encoding="utf-8")

        # ── Auto-fix: add @pytest.mark.asyncio to async test functions ──────
        # Gemini frequently writes `async def test_*` without the decorator.
        # Without it (or asyncio_mode=auto), pytest collects the function but
        # the coroutine is never awaited → every async test fails on first run.
        try:
            _gen_tree = ast.parse(code)
            _gen_lines = code.splitlines()
            _gen_inserts = []  # (line_index_to_insert_before, indent_str)

            def _gen_has_asyncio_marker(fn_node) -> bool:
                for _d in fn_node.decorator_list:
                    if isinstance(_d, ast.Attribute) and _d.attr == "asyncio":
                        return True
                    if isinstance(_d, ast.Call) and isinstance(_d.func, ast.Attribute) and _d.func.attr == "asyncio":
                        return True
                return False

            for _gen_fn in ast.walk(_gen_tree):
                if not isinstance(_gen_fn, ast.AsyncFunctionDef):
                    continue
                if not _gen_fn.name.startswith("test_"):
                    continue
                if _gen_has_asyncio_marker(_gen_fn):
                    continue
                _dec_start = (
                    min(d.lineno for d in _gen_fn.decorator_list) - 1
                    if _gen_fn.decorator_list else _gen_fn.lineno - 1
                )
                _indent = re.match(r'^(\s*)', _gen_lines[_dec_start]).group(1)
                _gen_inserts.append((_dec_start, _indent))

            if _gen_inserts:
                for _ins_line, _ins_indent in sorted(_gen_inserts, reverse=True):
                    _gen_lines.insert(_ins_line, f"{_ins_indent}@pytest.mark.asyncio")
                _fixed_gen = "\n".join(_gen_lines)
                ast.parse(_fixed_gen)  # validate before writing
                test_path.write_text(_fixed_gen, encoding="utf-8")
                code = _fixed_gen
                await _emit(log_cb, "success",
                    f"✅ Auto-fix: added @pytest.mark.asyncio to {len(_gen_inserts)} async test(s)")
        except Exception as _gen_async_exc:
            await _emit(log_cb, "warn", f"Auto-fix (asyncio marker) skipped: {_gen_async_exc}")

        # Verify at least one test function exists
        if "def test_" not in code:
            await _emit(log_cb, "warn", "Generated test file contains no test_ functions")
            return {"status": "fail", "output": "No test functions found in generated code"}

        quality = analyze_file(str(test_path))
        await _emit(log_cb, "success", f"✓ {test_path.name} ({quality.get('line_count', 0)} lines)")

        # ── Generate additional test files from package_structure ──
        # Skip when called as regeneration fix — only test_connector.py matters for passing pytest.
        if is_regen:
            return {
                "status": "pass",
                "output": {
                    "file": str(test_path),
                    "line_count": quality.get("line_count", 0),
                    "regenerated": True,
                },
            }

        package_structure = context.get("package_structure", {})
        pkg_files = package_structure.get("files", [])

        extra_test_py = [
            f for f in pkg_files
            if f.get("path", "").startswith("tests/")
            and f.get("path", "").endswith(".py")
            and f.get("path") not in ("tests/test_connector.py", "tests/__init__.py")
        ]

        fixture_files = [
            f for f in pkg_files
            if f.get("path", "").startswith("tests/fixtures/")
            and f.get("path", "").endswith(".json")
        ]

        total_extra = len(extra_test_py) + len(fixture_files)
        done_extra = 0
        if extra_test_py:
            await _emit(log_cb, "info", f"Generating {len(extra_test_py)} additional test file(s) from package structure...")

        for file_info in extra_test_py:
            file_path = file_info.get("path", "")
            # Strip any accidental "{service}_connector/" prefix
            file_path = re.sub(r'^[^/]+_connector/', '', file_path)
            file_desc = file_info.get("description", f"Tests for {context.get('service_name', 'service')}")
            if not file_path:
                continue

            done_extra += 1
            await _emit(log_cb, "info", f"  [{done_extra}/{total_extra}] Generating {file_path}...")

            test_mod_system = (await _get_prompt("TEST_MODULE_SYSTEM_PROMPT", TEST_MODULE_SYSTEM_PROMPT)).format(
                provider=_get_ctx(context, "provider", "unknown"),
                service_name=_get_ctx(context, "service_name", "Unknown Service"),
                connector_name=_get_ctx(context, "connector_name", _get_ctx(context, "service_name")),
                auth_type=_get_ctx(context, "auth_type", "unknown"),
                user_prompt=_get_ctx(context, "user_prompt", "(not provided)"),
                step_memory_summary=_build_step_memory_summary(context),
                file_path=file_path,
                file_description=file_desc,
                connector_code=connector_code,
                class_name=_get_ctx(context, "class_name", "Connector"),
            )

            try:
                test_mod_code = await call_llm_fix(
                    [{"role": "user", "content": (
                        f"Output the complete Python test source code for {file_path}. "
                        f"Return ONLY raw Python — no prose, no tools, no file operations. "
                        f"Keep docstrings to one line to avoid truncation."
                    )}],
                    system=test_mod_system,
                    max_tokens=60000,
                    on_chunk=_make_gemini_progress_cb(log_cb, f"Gemini {file_path}"),
                )
                test_mod_code = _clean_llm_code_response(test_mod_code)

                if not test_mod_code or len(test_mod_code) < 30 or not test_mod_code.lstrip().startswith(_VALID_PYTHON_STARTS):
                    await _emit(log_cb, "warn", f"  ⚠ {file_path}: LLM returned invalid code — skipping")
                    continue

                # Syntax check — retry once if broken
                try:
                    ast.parse(test_mod_code)
                except SyntaxError as syn_exc:
                    await _emit(log_cb, "warn", f"  ⚠ {file_path}: syntax error at line {syn_exc.lineno} — retrying...")
                    retry_msg = [{"role": "user", "content": (
                        f"The previous output for {file_path} was truncated and has a syntax error at line {syn_exc.lineno}: {syn_exc.msg}\n\n"
                        f"Broken code:\n```python\n{test_mod_code}\n```\n\n"
                        f"Output the COMPLETE, CORRECTED Python test file. "
                        f"Use only single-line # comments — NO multi-line triple-quoted strings. No markdown fences."
                    )}]
                    test_mod_code = await call_llm_fix(retry_msg, system=test_mod_system, max_tokens=60000,
                                                       on_chunk=_make_gemini_progress_cb(log_cb, f"Gemini retry {file_path}"))
                    test_mod_code = _clean_llm_code_response(test_mod_code)
                    try:
                        ast.parse(test_mod_code)
                    except SyntaxError as exc2:
                        await _emit(log_cb, "warn", f"  ⚠ {file_path}: still broken after retry ({exc2}) — skipping")
                        continue

                test_mod_path = out_dir / file_path
                test_mod_path.parent.mkdir(parents=True, exist_ok=True)
                test_mod_path.write_text(test_mod_code, encoding="utf-8")
                # Apply same auto-fixes to conftest.py / extra test files that we apply to test_connector.py
                try:
                    from integration.services.code_quality import auto_fix_python_file as _afpf2
                    _afpf2(test_mod_path)
                except Exception:
                    pass
                await _emit(log_cb, "success", f"  ✓ {file_path} ({test_mod_code.count(chr(10)) + 1} lines)")

            except Exception as exc:
                await _emit(log_cb, "warn", f"  ⚠ {file_path}: generation failed — {exc}")

        # ── Generate JSON fixture files ──
        if fixture_files:
            await _emit(log_cb, "info", f"Generating {len(fixture_files)} JSON fixture file(s)...")
            fixtures_dir = out_dir / "tests" / "fixtures"
            fixtures_dir.mkdir(parents=True, exist_ok=True)
            # Ensure __init__.py in fixtures dir
            (fixtures_dir / "__init__.py").write_text("", encoding="utf-8")

        for file_info in fixture_files:
            file_path = file_info.get("path", "")
            done_extra += 1
            if file_path:
                await _emit(log_cb, "info", f"  [{done_extra}/{total_extra}] Generating fixture {file_path}...")
            file_desc = file_info.get("description", "API response fixture")
            if not file_path:
                continue

            fixture_prompt = (
                f"Generate a realistic sample JSON fixture file for the {context.get('service_name', 'service')} API.\n"
                f"File: {file_path}\n"
                f"Purpose: {file_desc}\n"
                f"Provider: {context.get('provider', 'unknown')}\n\n"
                f"Return ONLY valid JSON. No markdown fences. No explanations."
            )
            try:
                fixture_raw = await call_llm_tests(
                    [{"role": "user", "content": fixture_prompt}],
                    max_tokens=1024,
                    expect_code=False,
                )
                fixture_raw = fixture_raw.strip()
                # Strip markdown fences if LLM included them
                if fixture_raw.startswith("```"):
                    lines = fixture_raw.split("\n")
                    fixture_raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                # Validate JSON
                json.loads(fixture_raw)
                fixture_path = out_dir / file_path
                fixture_path.parent.mkdir(parents=True, exist_ok=True)
                fixture_path.write_text(fixture_raw, encoding="utf-8")
                await _emit(log_cb, "success", f"  ✓ {file_path}")

            except json.JSONDecodeError:
                await _emit(log_cb, "warn", f"  ⚠ {file_path}: LLM returned invalid JSON — writing empty fixture")
                fixture_path = out_dir / file_path
                fixture_path.parent.mkdir(parents=True, exist_ok=True)
                fixture_path.write_text("{}", encoding="utf-8")
            except Exception as exc:
                await _emit(log_cb, "warn", f"  ⚠ {file_path}: fixture generation failed — {exc}")

        # ── Upload test files to R2 ─────────────────────────────────────────
        try:
            _r2_count = await r2_service.upload_connector_dir(
                context["tenant_id"], context["service_slug"], context["session_id"], out_dir
            )
            await _emit(log_cb, "success", f"  ↑ {_r2_count} file(s) synced to R2 (including tests)")
        except Exception as _r2_err:
            await _emit(log_cb, "warn", f"  R2 upload skipped: {_r2_err}")

        return {
            "status": "pass",
            "output": {
                "file": str(test_path),
                "line_count": quality.get("line_count", 0),
                "extra_tests_generated": len(extra_test_py),
                "fixtures_generated": len(fixture_files),
            },
        }
    except Exception as exc:
        await _emit(log_cb, "error", f"Test generation failed: {exc}")
        return {"status": "fail", "output": str(exc)}


# ── run_tests ────────────────────────────────────────────────────────

async def handle_run_tests(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Run pytest on generated test files.

    Pre-validates connector.py and test_connector.py before running pytest.
    Returns ``root_cause`` in the output dict so auto-run can route fixes
    to the correct file instead of blindly fixing tests.
    """
    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    tests_dir = out_dir / "tests"

    if not tests_dir.exists():
        await _emit(log_cb, "error", "tests/ directory not found")
        return {"status": "fail", "output": {"root_cause": "tests_missing"}}

    # ── Pre-run file validation ──
    file_status = validate_generated_files(out_dir)

    if not file_status["connector"]["valid"]:
        reason = file_status["connector"]["reason"]
        preview = file_status["connector"].get("content_preview", file_status["connector"].get("error", ""))
        await _emit(log_cb, "error", f"connector.py is invalid ({reason}) — cannot run tests. {preview}")
        return {
            "status": "fail",
            "output": {
                "root_cause": "connector_invalid",
                "reason": reason,
                "detail": preview,
                "passed": 0, "failed": 0, "errors": 0, "returncode": -1, "stdout": "",
            },
        }

    # ── Pre-run: sync __init__.py class name with connector.py ──
    # This prevents ImportError when __init__.py uses a different class name
    # than what the LLM actually generated in connector.py.
    actual_class = _sync_init_with_connector(out_dir, context)
    expected_class = context.get("class_name", "Connector")
    if actual_class and actual_class != expected_class:
        await _emit(log_cb, "info", f"Auto-fixed __init__.py: '{expected_class}' → '{actual_class}'")

    if not file_status["tests"]["valid"]:
        reason = file_status["tests"]["reason"]
        preview = file_status["tests"].get("content_preview", file_status["tests"].get("error", ""))
        await _emit(log_cb, "error", f"test_connector.py is invalid ({reason}) — cannot run tests. {preview}")
        return {
            "status": "fail",
            "output": {
                "root_cause": "tests_invalid",
                "reason": reason,
                "detail": preview,
                "passed": 0, "failed": 0, "errors": 0, "returncode": -1, "stdout": "",
            },
        }

    # ── Run pytest with live streaming ──
    await _emit(log_cb, "info", f"🧪 Running test suite...")

    # Build a thread-safe line emitter: _pytest_run_sync calls on_line(line) from
    # a worker thread; we post each line back into the asyncio event loop via
    # call_soon_threadsafe so log_cb (a coroutine) can be scheduled safely.
    loop = asyncio.get_event_loop()

    def _on_pytest_line(line: str) -> None:
        if not log_cb or not line.strip():
            return
        # Classify the line so the UI colours it correctly
        if " PASSED" in line:
            level = "success"
        elif " FAILED" in line or line.startswith("FAILED "):
            level = "error"
        elif line.startswith("E ") or "ERROR" in line:
            level = "error"
        elif line.startswith("WARNING") or "warning" in line.lower():
            level = "warn"
        else:
            level = "info"
        loop.call_soon_threadsafe(
            lambda l=level, m=line: asyncio.ensure_future(log_cb(l, m))
        )

    result = await _pytest_run_async( tests_dir, out_dir, _on_pytest_line)

    # Build method-level test results for UI tracking
    _test_file = tests_dir / "test_connector.py"
    _method_test_map = _build_method_test_map(_test_file)

    def _compute_method_results(details: List[Dict[str, Any]]) -> Dict[str, Any]:
        mr: Dict[str, Any] = {}
        for method, test_funcs in _method_test_map.items():
            method_tests = []
            m_passed = 0
            m_failed = 0
            for detail in details:
                func_name = detail["test"].split("::")[-1] if "::" in detail["test"] else detail["test"]
                if func_name in test_funcs:
                    method_tests.append({"name": func_name, "status": detail["status"], "node_id": detail["test"]})
                    if detail["status"] == "passed":
                        m_passed += 1
                    else:
                        m_failed += 1
            mr[method] = {"tests": method_tests, "passed": m_passed, "failed": m_failed}
        return mr

    if result["returncode"] == 0:
        await _emit(log_cb, "success", f"✅ All tests passed! ({result['passed']} passed)")
        return {
            "status": "pass",
            "output": {
                "root_cause": "none",
                "passed": result["passed"],
                "failed": result["failed"],
                "errors": result["errors"],
                "returncode": 0,
                "stdout": result["output"][-2000:] if result["output"] else "",
                "details": result.get("details", []),
                "method_results": _compute_method_results(result.get("details", [])),
            },
        }

    # ── Classify failure root cause from pytest output ──
    output_text = result["output"] or ""
    await _emit(log_cb, "error", f"Tests: {result['passed']} passed, {result['failed']} failed, {result['errors']} errors (exit code {result['returncode']})")
    # Emit the actual pytest output so the developer can see what went wrong
    # Lines already streamed live above — no need to dump bulk output here

    if "ImportError" in output_text or "ModuleNotFoundError" in output_text:
        # Check for __init__.py class name mismatch (e.g. "cannot import name 'X' from 'pkg.connector'")
        if "cannot import name" in output_text and "connector" in output_text:
            # Try to auto-fix __init__.py class mismatch right now
            fixed_class = _sync_init_with_connector(out_dir, context)
            if fixed_class:
                await _emit(log_cb, "info", f"Detected class name mismatch in __init__.py — fixed to '{fixed_class}'. Re-running tests...")
                # Re-run pytest immediately after the fix
                rerun_result = await _pytest_run_async( tests_dir, out_dir)
                if rerun_result["returncode"] == 0:
                    await _emit(log_cb, "success", f"All tests passed after __init__.py fix! ({rerun_result['passed']} passed)")
                    return {
                        "status": "pass",
                        "output": {
                            "root_cause": "none",
                            "auto_fixed": "init_class_mismatch",
                            "passed": rerun_result["passed"],
                            "failed": rerun_result["failed"],
                            "errors": rerun_result["errors"],
                            "returncode": 0,
                            "stdout": rerun_result["output"][-2000:] if rerun_result["output"] else "",
                            "details": rerun_result.get("details", []),
                            "method_results": _compute_method_results(rerun_result.get("details", [])),
                        },
                    }
                # Re-run still failed — fall through to normal classification
                output_text = rerun_result["output"] or ""
                result = rerun_result
                await _emit(log_cb, "warn", f"Tests still failing after __init__.py fix — classifying remaining errors")

            root_cause = "connector_import_error"
        elif "from connector" in output_text or "import connector" in output_text or "from .connector" in output_text:
            root_cause = "connector_import_error"
        else:
            root_cause = "tests_import_error"
    elif result["returncode"] in (4, 5) or "collected 0 items" in output_text or "no tests ran" in output_text.lower():
        # Exit code 4 = usage/path error (bad path arg), 5 = no tests collected
        root_cause = "tests_invalid"
        await _emit(log_cb, "error", "No tests were collected — test file may be empty or have no test_ functions")
    elif result["errors"] > 0 and result["passed"] == 0 and result["failed"] == 0:
        root_cause = "collection_error"
    elif result["failed"] > 0:
        root_cause = "test_failures"
    else:
        root_cause = "unknown"

    return {
        "status": "fail",
        "output": {
            "root_cause": root_cause,
            "passed": result["passed"],
            "failed": result["failed"],
            "errors": result["errors"],
            "returncode": result["returncode"],
            "stdout": output_text[-5000:],
            "details": result.get("details", []),
            "method_results": _compute_method_results(result.get("details", [])),
        },
    }


# ── AI fix handlers ──────────────────────────────────────────────────

async def handle_fix_connector(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Use LLM to fix errors in the generated connector.py."""
    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    connector_path = out_dir / "connector.py"

    if not connector_path.exists():
        await _emit(log_cb, "error", "connector.py not found — cannot fix")
        return {"status": "fail", "output": "connector.py missing"}

    current_code = connector_path.read_text(encoding="utf-8")
    error_details = context.get("error_details", "Unknown error")

    await _emit(log_cb, "info", "⚡ Sending code + error details to Gemini for fix...")

    # Read installed packages for full context
    _fix_out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    _fix_req = _fix_out_dir / "requirements.txt"
    _fix_pkgs = _fix_req.read_text(encoding="utf-8").strip() if _fix_req.exists() else "(not found)"
    _fix_prev = context.get("previous_fix_summaries", [])
    _fix_prev_str = (
        "\n".join(f"  Attempt {i+1}: {s}" for i, s in enumerate(_fix_prev))
        if _fix_prev else "  None — this is the first attempt."
    )

    system = await _inject_rag_context((await _get_prompt("FIX_CODE_PROMPT", FIX_CODE_PROMPT)).format(
        current_code=current_code,
        error_details=error_details,
        provider=_get_ctx(context, "provider", "unknown"),
        service_name=_get_ctx(context, "service_name", "Unknown Service"),
        connector_name=_get_ctx(context, "connector_name", _get_ctx(context, "service_name")),
        auth_type=_get_ctx(context, "auth_type", "unknown"),
        sdk_package=_get_ctx(context, "sdk_package"),
        user_prompt=_get_ctx(context, "user_prompt", "(not provided)"),
        fix_attempt=str(context.get("fix_attempt", 1)),
        step_memory_summary=_build_step_memory_summary(context),
        previous_fix_summary=_fix_prev_str,
        installed_packages=_fix_pkgs,
        base_connector_interface=BASE_CONNECTOR_INTERFACE,
    ), context)

    messages = [
        {"role": "user", "content": "Output the complete corrected Python source code for this connector. Fix all errors shown. Return ONLY raw Python — no prose, no tools, no file operations."},
    ]

    try:
        code = await call_llm_fix(messages, system=system, max_tokens=35000,
                                   on_chunk=_make_gemini_progress_cb(log_cb, "Gemini fix"))
        code = _clean_llm_code_response(code)

        # Guard: reject obviously non-Python LLM responses
        if not code or len(code) < 50 or (not code.lstrip().startswith(_VALID_PYTHON_STARTS)):
            await _emit(log_cb, "error", f"LLM returned invalid response for fix: {code[:120]}")
            return {"status": "fail", "output": f"LLM fix did not return valid Python code: {code[:200]}"}

        # Validate syntax
        try:
            ast.parse(code)
            await _emit(log_cb, "success", "Fixed code passes syntax validation")
        except SyntaxError as exc:
            # Try to auto-recover if the LLM output was truncated mid-string
            recovered = _try_recover_truncated_code(code, exc)
            if recovered:
                try:
                    ast.parse(recovered)
                    await _emit(log_cb, "warn", f"Auto-recovered truncated LLM output (closed unterminated string)")
                    code = recovered
                except SyntaxError as exc2:
                    await _emit(log_cb, "error", f"Fixed code still has syntax error after recovery attempt: {exc2} — NOT overwriting connector.py")
                    return {"status": "fail", "output": f"Fixed code has syntax error: {exc2}"}
            else:
                await _emit(log_cb, "error", f"Fixed code still has syntax error: {exc} — NOT overwriting connector.py")
                # DO NOT write broken code — preserve the working connector.py
                return {"status": "fail", "output": f"Fixed code has syntax error: {exc}"}

        connector_path.write_text(code, encoding="utf-8")

        # Sync __init__.py with actual class name after fix
        actual_class = _sync_init_with_connector(out_dir, context)
        if actual_class:
            await _emit(log_cb, "info", f"__init__.py synced with class: {actual_class}")

        quality = analyze_file(str(connector_path))

        await _emit(log_cb, "success", f"Fixed connector written → {connector_path.name} ({quality.get('line_count', 0)} lines, score: {quality.get('quality_score', 0)})")
        return {
            "status": "pass",
            "output": {
                "file": str(connector_path),
                "line_count": quality.get("line_count", 0),
                "quality_score": quality.get("quality_score", 0),
                "fixed": True,
            },
        }
    except Exception as exc:
        await _emit(log_cb, "error", f"Fix generation failed: {exc}")
        return {"status": "fail", "output": str(exc)}


async def handle_fix_connector_for_tests(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """TDD fix: update connector.py so it satisfies the failing tests.

    When TEST_LLM_MODE=gemini, uses an agentic loop (Gemini + tool calls) so the
    model reads both connector.py and tests, writes fixes, and runs pytest itself.

    Called when tests fail due to behavioural mismatches (test_failures root cause).
    Tests are the spec — the connector must be changed to pass them.
    """
    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    connector_path = out_dir / "connector.py"
    test_path = out_dir / "tests" / "test_connector.py"

    if not connector_path.exists():
        await _emit(log_cb, "error", "connector.py not found — cannot fix")
        return {"status": "fail", "output": "connector.py missing"}

    # ── Agentic fix: Gemini + tool calls ────────────────────────────────────
    if False:  # gemini path disabled — Claude is the only backend codegen runtime
        from integration.services.agentic_fix import gemini_agentic_fix, gemini_agentic_fix_connector
        from integration.services import knowledge_service as _ks
        tests_dir = out_dir / "tests"
        await _emit(log_cb, "info", f"🤖 Using {_llm_label()} agentic fix (tool-call loop)...")

        _tenant_id = context.get("tenant_id", "")
        _provider  = context.get("provider", "")
        _service   = context.get("service_slug", context.get("service_name", ""))

        async def _knowledge_fn(query: str) -> str:
            try:
                return await _ks.query_knowledge(
                    query=query, tenant_id=_tenant_id,
                    provider=_provider, service=_service, top_k=8,
                ) or ""
            except Exception:
                return ""

        # Load test guidelines — used in both phases
        _fix_attempt_num = context.get("fix_attempt", 1)
        _test_guidelines = ""
        if _fix_attempt_num <= 1:
            _test_guidelines = await _load_test_guidelines(out_dir, _provider, _service)
            if _test_guidelines:
                await _emit(log_cb, "info",
                    f"📋 Test guidelines loaded ({len(_test_guidelines)} chars) — injected into fix prompts")
            else:
                await _emit(log_cb, "info", "ℹ No test guidelines found — using generic fix prompt")
        else:
            await _emit(log_cb, "info", f"ℹ Fix attempt {_fix_attempt_num} — skipping test guidelines (already applied on attempt 1)")

        # Run fresh pytest to get latest output
        fresh = await _pytest_run_async(tests_dir, out_dir)
        _fresh_output = fresh.get("output", context.get("error_details", ""))

        # ── Parse target methods for -k filter (same logic as handle_fix_tests) ──
        import re as _re_meth2
        _error_details_ctx = context.get("error_details", "")
        _target_methods2: list[str] = []
        _single2 = _re_meth2.search(r"Fixing ONLY method:\s*(\w+)", _error_details_ctx)
        if _single2:
            _target_methods2 = [_single2.group(1).strip()]
        else:
            _multi2 = _re_meth2.search(r"method test\(s\) failing:\s*([^\n]+)", _error_details_ctx)
            if _multi2:
                _target_methods2 = [m.strip() for m in _multi2.group(1).split(",") if m.strip()]
        if _target_methods2:
            await _emit(log_cb, "info",
                f"🎯 Targeting methods: {', '.join(_target_methods2)} — run_tests() will use -k filter")

        # ── Agentic fix: fix connector + client files to make tests pass ──────
        await _emit(log_cb, "info", "Agentic fix: analysing failures and fixing connector...")
        result = await gemini_agentic_fix_connector(
            out_dir,
            initial_pytest_output=_fresh_output,
            knowledge_fn=_knowledge_fn,
            tenant_id=_tenant_id,
            provider=_provider,
            service=_service,
            test_guidelines=_test_guidelines,
            log_cb=log_cb,
            max_iterations=40,
            target_methods=_target_methods2 or None,
        )
        if result["success"]:
            rerun = await _pytest_run_async(tests_dir, out_dir)
            return {
                "status": "pass" if rerun["returncode"] == 0 else "fail",
                "output": {
                    "root_cause": "none" if rerun["returncode"] == 0 else "test_failures",
                    "passed": rerun["passed"],
                    "failed": rerun["failed"],
                    "errors": rerun["errors"],
                    "returncode": rerun["returncode"],
                    "stdout": rerun["output"][-3000:],
                    "agentic_iterations": result["iterations"],
                },
            }
        else:
            await _emit(log_cb, "warn", f"Agentic fix did not fully pass ({result['message']}) — falling back to prompt-based fix")

    current_code = connector_path.read_text(encoding="utf-8")

    # ── Run pytest live to get fresh full traceback output ──
    tests_dir = out_dir / "tests"
    await _emit(log_cb, "info", "Running tests to collect live traceback output for Gemini...")
    live_result = await _pytest_run_async( tests_dir, out_dir)
    live_output = live_result.get("output", "")
    live_failed = live_result.get("failed", 0)
    live_errors = live_result.get("errors", 0)

    # Use live counts if we have them
    test_failed_count = live_failed or context.get("test_failed", "?")
    test_errors_count = live_errors or context.get("errors", 0)

    # Collect ALL test files (test_connector.py + any extras like test_auth.py)
    all_test_files = sorted(tests_dir.glob("test_*.py")) if tests_dir.exists() else []
    if test_path in all_test_files:
        all_test_files = [test_path] + [f for f in all_test_files if f != test_path]

    test_parts = []
    for tf in all_test_files:
        test_parts.append(f"# ── {tf.name} ──\n{tf.read_text(encoding='utf-8')}")
    failing_tests_code = "\n\n".join(test_parts) if test_parts else "# no test files found"

    # Build error_details from live pytest output — full tracebacks, not filtered snippets
    error_details = (
        f"{test_failed_count} test(s) failed, {test_errors_count} collection error(s).\n\n"
        f"Full pytest output (--tb=short):\n{live_output}"
    )

    await _emit(log_cb, "info",
        f"TDD fix: sending all {test_failed_count} failures + full tracebacks to Gemini "
        f"({len(all_test_files)} test file(s))...")

    # ── Gather full connector context for Gemini ──────────────────────────────────────────
    _tdd_provider     = context.get("provider", "unknown")
    _tdd_service      = context.get("service", context.get("service_slug", "unknown"))
    _tdd_conn_name    = context.get("connector_name", "") or _tdd_service
    _tdd_auth_type    = context.get("auth_type", "unknown")
    _tdd_user_prompt  = context.get("user_prompt", "(not available)")
    _tdd_fix_attempt  = context.get("fix_attempt", 1)

    _tdd_prev_attempts = context.get("previous_fix_summaries", [])
    _tdd_prev_str = (
        "\n".join(f"  Attempt {i+1}: {s}" for i, s in enumerate(_tdd_prev_attempts))
        if _tdd_prev_attempts else "  None — this is the first fix attempt."
    )

    _tdd_req_path = out_dir / "requirements.txt"
    _tdd_pkgs = _tdd_req_path.read_text(encoding="utf-8").strip() if _tdd_req_path.exists() else "(not found)"

    _tdd_json_path = out_dir / "connector.json"
    _tdd_json_str = "(connector.json not found)"
    if _tdd_json_path.exists():
        try:
            import json as _json_tdd
            _tdd_json_str = _json_tdd.dumps(_json_tdd.loads(_tdd_json_path.read_text(encoding="utf-8")), indent=2)
        except Exception:
            _tdd_json_str = _tdd_json_path.read_text(encoding="utf-8")

    _tdd_base_path = Path(__file__).resolve().parent.parent.parent / "shared" / "base_connector.py"
    _tdd_base_str = "(base_connector.py not found)"
    if _tdd_base_path.exists():
        try:
            _tdd_base_src = _tdd_base_path.read_text(encoding="utf-8")
            _tdd_base_tree = ast.parse(_tdd_base_src)
            _tdd_base_lines = _tdd_base_src.splitlines()
            _tdd_sigs = []
            for _tbn in ast.walk(_tdd_base_tree):
                if isinstance(_tbn, ast.ClassDef) and "Connector" in _tbn.name:
                    for _tbm in _tbn.body:
                        if isinstance(_tbm, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            _sig_end = _tbm.body[0].lineno - 1 if _tbm.body else _tbm.lineno
                            _tdd_sigs.append("\n".join(_tdd_base_lines[_tbm.lineno - 1:_sig_end]))
            _tdd_base_str = "\n".join(_tdd_sigs) if _tdd_sigs else _tdd_base_src[:3000]
        except Exception:
            _tdd_base_str = _tdd_base_path.read_text(encoding="utf-8")[:3000]

    system = await _inject_rag_context((await _get_prompt("FIX_CONNECTOR_FOR_TESTS_PROMPT", FIX_CONNECTOR_FOR_TESTS_PROMPT)).format(
        provider=_tdd_provider,
        service=_tdd_service,
        connector_name=_tdd_conn_name,
        auth_type=_tdd_auth_type,
        user_prompt=_tdd_user_prompt,
        fix_attempt=_tdd_fix_attempt,
        previous_fix_summary=_tdd_prev_str,
        base_connector_interface=_tdd_base_str,
        installed_packages=_tdd_pkgs,
        connector_json=_tdd_json_str,
        failing_tests_code=failing_tests_code,
        current_code=current_code,
        error_details=error_details,
        step_memory_summary=_build_step_memory_summary(context),
    ), context)

    messages = [
        {"role": "user", "content":
            "Output the complete corrected connector.py that satisfies all the failing tests above. "
            "Return ONLY raw Python — no prose, no tools, no file operations."},
    ]

    try:
        code = await call_llm_fix(messages, system=system, max_tokens=35000,
                                   on_chunk=_make_gemini_progress_cb(log_cb, "Gemini TDD fix"))
        code = _clean_llm_code_response(code)

        if not code or len(code) < 50 or not code.lstrip().startswith(_VALID_PYTHON_STARTS):
            await _emit(log_cb, "error", f"LLM returned invalid response: {code[:120]}")
            return {"status": "fail", "output": f"LLM did not return valid Python code: {code[:200]}"}

        try:
            ast.parse(code)
            await _emit(log_cb, "success", "TDD-fixed connector passes syntax validation")
        except SyntaxError as exc:
            await _emit(log_cb, "error", f"Fixed connector has syntax error: {exc} — NOT overwriting connector.py")
            # DO NOT write broken code — preserve the working connector.py
            return {"status": "fail", "output": f"Syntax error in fixed connector: {exc}"}

        connector_path.write_text(code, encoding="utf-8")
        actual_class = _sync_init_with_connector(out_dir, context)
        if actual_class:
            await _emit(log_cb, "info", f"__init__.py synced with class: {actual_class}")

        quality = analyze_file(str(connector_path))
        await _emit(log_cb, "success",
            f"Connector updated → {connector_path.name} ({quality.get('line_count', 0)} lines, "
            f"score: {quality.get('quality_score', 0)})")
        return {
            "status": "pass",
            "output": {
                "file": str(connector_path),
                "line_count": quality.get("line_count", 0),
                "quality_score": quality.get("quality_score", 0),
                "tdd_fix": True,
            },
        }
    except Exception as exc:
        await _emit(log_cb, "error", f"TDD connector fix failed: {exc}")
        return {"status": "fail", "output": str(exc)}


async def _fast_fix_test_collection_errors(
    out_dir: Path,
    error_details: str,
    log_cb: LogCallback = None,
) -> bool:
    """Deterministically fix mechanical test-file errors without any LLM call.

    Handles:
      1. Broken / orphaned import continuations (IndentationError)
      2. Wrong class name  (hallucinated by Gemini during guidelines gen)
      3. Wrong exception names (hallucinated)
      4. Wrong import paths  (e.g. 'from connector import' → package path)
      5. ModuleNotFoundError for the connector package itself

    Returns True if at least one change was made.
    """
    import re as _re
    import ast as _ast

    tests_dir = out_dir / "tests"
    test_file = tests_dir / "test_connector.py"
    if not test_file.exists():
        return False

    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            return ""

    connector_src = _read(out_dir / "connector.py")
    exceptions_src = _read(out_dir / "exceptions.py")
    metadata_src = _read(out_dir / "metadata" / "connector.json")

    original = _read(test_file)
    patched = original

    # ── 1. Find real class name ───────────────────────────────────────────────
    real_class_name = ""
    for line in connector_src.splitlines():
        m = _re.match(r"^class\s+(\w+)\s*\(.*BaseConnector.*\)", line.strip())
        if m:
            real_class_name = m.group(1)
            break

    # ── 2. Find real exception names ─────────────────────────────────────────
    real_exceptions: set = set()
    for src in (connector_src, exceptions_src):
        for line in src.splitlines():
            m = _re.match(r"^class\s+(\w+(?:Error|Exception))\s*\(", line.strip())
            if m:
                real_exceptions.add(m.group(1))

    package_name = out_dir.name  # e.g. paytm_upi_connector

    # ── 3. Fix broken import blocks (IndentationError) ───────────────────────
    # Pattern: a comment line followed by orphaned continuation items + closing )
    # e.g.
    #   # Corrected import statement...
    #       FooError, BarError, SomeConnector
    #   )
    # Fix: rebuild as a proper `from <package>.connector import (...)` block
    _import_comment_re = _re.compile(
        r"(# [^\n]*import[^\n]*\n)((?:[ \t]+\w[\w,\s]*\n)+)\)",
        _re.MULTILINE,
    )
    def _rebuild_import(m):
        # Extract names from the orphaned lines
        names_raw = m.group(2)
        names = [n.strip().rstrip(",") for n in _re.findall(r"\b(\w+)\b", names_raw)]
        # Split into connector class vs exceptions
        exc_names = [n for n in names if n.endswith("Error") or n.endswith("Exception")]
        conn_names = [n for n in names if n not in exc_names]
        # Map hallucinated names to real ones
        if real_class_name:
            conn_names = [real_class_name if n.endswith("Connector") and n != "BaseConnector" else n
                          for n in conn_names]
        fixed_exc = []
        for e in exc_names:
            if e in real_exceptions:
                fixed_exc.append(e)
            else:
                # Find best real match
                best = next((r for r in sorted(real_exceptions) if r[:4] == e[:4]), None)
                if best:
                    fixed_exc.append(best)
        all_names = list(dict.fromkeys(conn_names + fixed_exc))  # dedup, preserve order
        if not all_names:
            all_names = [real_class_name] if real_class_name else ["YourConnector"]
        imports_str = ",\n    ".join(all_names)
        return f"from {package_name}.connector import (\n    {imports_str}\n)"

    new_patched = _import_comment_re.sub(_rebuild_import, patched)
    if new_patched != patched:
        patched = new_patched
        await _emit(log_cb, "info", "⚡ Fast fix: repaired broken import block (IndentationError)")

    # ── 4. Ensure bare 'from connector import' (NOT full package path) ───────
    # The test runner sets cwd=<package>/ and includes that dir in PYTHONPATH.
    # bare `from connector import X` resolves to connector.py directly.
    # Using `from <package>.connector import X` creates a SECOND module instance
    # in sys.modules and breaks all isinstance / exception identity checks.
    # Convert any full-path imports back to bare 'connector' imports.
    patched = _re.sub(
        rf"\bfrom {_re.escape(package_name)}\.connector import\b",
        "from connector import",
        patched,
    )
    patched = _re.sub(
        rf"patch\(['\"]({_re.escape(package_name)})\.connector\.",
        "patch('connector.",
        patched,
    )

    # ── 5. Fix wrong class name everywhere ───────────────────────────────────
    if real_class_name:
        # Find all ConnectorClass-looking names that are NOT the real one
        fake_classes = set(_re.findall(r"\b(\w+Connector)\b", patched))
        fake_classes.discard(real_class_name)
        fake_classes.discard("BaseConnector")
        for fake in fake_classes:
            if fake not in connector_src:
                patched = patched.replace(fake, real_class_name)
                await _emit(log_cb, "info", f"⚡ Fast fix: {fake!r} → {real_class_name!r}")

    # ── 6. Fix hallucinated exception names ──────────────────────────────────
    _KNOWN_EXTERNAL = {
        "ConnectError", "TimeoutException", "RequestError", "HTTPStatusError",
        "Exception", "RuntimeError", "ValueError", "KeyError",
        "TypeError", "ImportError", "AttributeError",
    }
    if real_exceptions:
        fake_excs = set(_re.findall(r"\b(\w+(?:Error|Exception))\b", patched))
        for exc in fake_excs:
            if exc in _KNOWN_EXTERNAL or exc in real_exceptions:
                continue
            if exc not in connector_src and exc not in exceptions_src:
                best = next(
                    (r for r in sorted(real_exceptions) if r[:4] == exc[:4]),
                    next(iter(sorted(real_exceptions)), None),
                )
                if best:
                    patched = patched.replace(exc, best)
                    await _emit(log_cb, "info", f"⚡ Fast fix: exception {exc!r} → {best!r}")

    # ── 7. Verify the patch doesn't have syntax errors ────────────────────────
    try:
        _ast.parse(patched)
    except SyntaxError as _se:
        # Patch made things worse or has its own syntax error — don't apply
        await _emit(log_cb, "warn", f"⚡ Fast fix produced syntax error ({_se}) — reverting")
        return False

    if patched == original:
        return False  # nothing changed

    test_file.write_text(patched, encoding="utf-8")
    return True


async def handle_fix_tests(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Fix STRUCTURAL errors in the test file (imports, class names, mock wiring).

    When TEST_LLM_MODE=gemini, uses an agentic loop (Gemini + tool calls) so the
    model reads files, writes fixes, and runs pytest itself until tests pass.

    For behavioural failures (test_failures), use handle_fix_connector_for_tests instead.
    Tests are the TDD spec — their assertions must never be weakened to match a broken connector.
    """
    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    tests_dir = out_dir / "tests"
    connector_path = out_dir / "connector.py"
    error_details = context.get("error_details", "Unknown error")

    if not tests_dir.exists():
        await _emit(log_cb, "error", "tests/ directory not found — cannot fix")
        return {"status": "fail", "output": "tests/ missing"}

    # ── Fast pre-fix: deterministic repair BEFORE calling Gemini ─────────────
    # Collection errors (IndentationError, ImportError, wrong class names) are
    # trivially mechanical — no LLM needed. Fix them here in <1s so Gemini
    # only runs when the problem actually requires intelligence.
    _fast_fix_applied = await _fast_fix_test_collection_errors(
        out_dir, error_details, log_cb
    )
    if _fast_fix_applied:
        # Re-run pytest to see if the fast fix was enough
        _fast_rerun = await _pytest_run_async( tests_dir, out_dir)
        if _fast_rerun["returncode"] == 0 or _fast_rerun["errors"] == 0:
            await _emit(log_cb, "info", "✓ Fast pre-fix resolved all collection errors")
            return {
                "status": "pass" if _fast_rerun["returncode"] == 0 else "fail",
                "output": {
                    "root_cause": "none" if _fast_rerun["returncode"] == 0 else "test_failures",
                    "passed": _fast_rerun["passed"],
                    "failed": _fast_rerun["failed"],
                    "errors": _fast_rerun["errors"],
                    "returncode": _fast_rerun["returncode"],
                    "stdout": _fast_rerun["output"][-3000:],
                    "fast_fix": True,
                },
            }
        await _emit(log_cb, "info", "Fast pre-fix applied but errors remain — handing off to Gemini agentic")

    # ── Agentic fix: Gemini + tool calls (read/write/run loop) ──────────────
    if False:  # gemini path disabled — Claude is the only backend codegen runtime
        from integration.services.agentic_fix import gemini_agentic_fix
        from integration.services import knowledge_service as _ks
        await _emit(log_cb, "info", f"🤖 Using {_llm_label()} agentic fix (tool-call loop)...")

        _tenant_id = context.get("tenant_id", "")
        _provider  = context.get("provider", "")
        _service   = context.get("service_slug", context.get("service_name", ""))

        # ── Run pytest fresh so Gemini sees the CURRENT real failures, not stale frontend state ──
        # The frontend sends its last-known pytest output which may be from a filtered/partial run.
        # Always run the full suite here so test_failures.md reflects ground truth.
        _fresh_pytest_output = error_details  # fallback to frontend-provided if pytest fails
        try:
            _fresh_run = await _pytest_run_async(tests_dir, out_dir)
            if _fresh_run.get("output"):
                _fresh_pytest_output = _fresh_run["output"]
                _f = _fresh_run.get("failed", 0)
                _p = _fresh_run.get("passed", 0)
                _e = _fresh_run.get("errors", 0)
                await _emit(log_cb, "info",
                    f"🔍 Fresh test run: {_p} passed, {_f} failed, {_e} errors — using this as ground truth for Gemini")
        except Exception as _fe:
            await _emit(log_cb, "warn", f"Fresh test run failed ({_fe}) — using frontend-provided output")

        # ── Store failures to disk so Gemini can read them via read_file ──
        # Gemini's read_file tool already has access to out_dir — writing test_failures.md
        # here means Gemini can open it at the start of the fix loop for full context.
        try:
            _failures_content = (
                f"# Test Failures — {_provider}/{_service}\n\n"
                f"These are the CURRENT pytest failures (fresh run, not stale state).\n"
                f"Read this file FIRST before writing any fix.\n\n"
                f"```\n{_fresh_pytest_output}\n```\n"
            )
            _local_failures = out_dir / "test_failures.md"
            _local_failures.write_text(_failures_content, encoding="utf-8")
            await _emit(log_cb, "info", f"📝 Test failures written to {_local_failures.name} for Gemini to read")
        except Exception:
            pass

        async def _knowledge_fn(query: str) -> str:
            try:
                return await _ks.query_knowledge(
                    query=query, tenant_id=_tenant_id,
                    provider=_provider, service=_service, top_k=8,
                ) or ""
            except Exception:
                return ""

        # Load connector-specific test guidelines — only on first fix attempt.
        # Re-injecting on subsequent attempts causes the LLM to over-constrain and loop.
        _fix_attempt_num_ft = context.get("fix_attempt", 1)
        _test_guidelines = ""
        if _fix_attempt_num_ft <= 1:
            _test_guidelines = await _load_test_guidelines(out_dir, _provider, _service)
            if _test_guidelines:
                await _emit(log_cb, "info",
                    f"📋 Test guidelines loaded ({len(_test_guidelines)} chars) — injected into fix prompt (attempt 1 only)")
            else:
                await _emit(log_cb, "info", "ℹ No test guidelines found — using generic fix prompt")
        else:
            await _emit(log_cb, "info", f"ℹ Fix attempt {_fix_attempt_num_ft} — skipping test guidelines")

        # ── Derive target methods from error_details ──────────────────────────
        # The frontend encodes "Fixing ONLY method: health_check" for single-method
        # fixes, and "X method test(s) failing: install, authorize, ..." for multi.
        # Extract these so run_tests() inside the loop uses -k filter — preventing
        # Gemini from seeing failures from OTHER methods and going off-track.
        import re as _re_meth
        _target_methods: list[str] = []
        _single = _re_meth.search(r"Fixing ONLY method:\s*(\w+)", error_details)
        if _single:
            _target_methods = [_single.group(1).strip()]
        else:
            _multi = _re_meth.search(
                r"method test\(s\) failing:\s*([^\n]+)", error_details
            )
            if _multi:
                _target_methods = [m.strip() for m in _multi.group(1).split(",") if m.strip()]

        if _target_methods:
            await _emit(log_cb, "info",
                f"🎯 Targeting methods: {', '.join(_target_methods)} — run_tests() will use -k filter")

        # Extract compile errors forwarded from the frontend compile-gate check.
        # These come prefixed with "COMPILE ERRORS IN CONNECTOR FILES" in error_details.
        import re as _re_compile
        _compile_errors = ""
        _compile_match = _re_compile.search(
            r"COMPILE ERRORS IN CONNECTOR FILES[^\n]*\n(.*?)(?=\n\n[A-Z]|\Z)",
            error_details,
            _re_compile.DOTALL,
        )
        if _compile_match:
            _compile_errors = _compile_match.group(0).strip()
            await _emit(log_cb, "info",
                f"🔍 Compile errors detected — injecting into Gemini context as CRITICAL priority")

        result = await gemini_agentic_fix(
            out_dir,
            initial_pytest_output=_fresh_pytest_output,  # always fresh — never stale frontend state
            knowledge_fn=_knowledge_fn,
            tenant_id=_tenant_id,
            provider=_provider,
            service=_service,
            test_guidelines=_test_guidelines,
            log_cb=log_cb,
            target_methods=_target_methods or None,
            compile_errors=_compile_errors,
        )
        if result["success"]:
            # Re-run pytest via standard runner to get structured pass/fail counts
            rerun = await _pytest_run_async( tests_dir, out_dir)
            return {
                "status": "pass" if rerun["returncode"] == 0 else "fail",
                "output": {
                    "root_cause": "none" if rerun["returncode"] == 0 else "test_failures",
                    "passed": rerun["passed"],
                    "failed": rerun["failed"],
                    "errors": rerun["errors"],
                    "returncode": rerun["returncode"],
                    "stdout": rerun["output"][-3000:],
                    "agentic_iterations": result["iterations"],
                },
            }
        else:
            await _emit(log_cb, "warn", f"Agentic fix did not fully pass ({result['message']}) — falling back to prompt-based fix")
            # Fall through to prompt-based fix below

    # ── Find which test file(s) have errors ──
    # Parse the error_details/traceback to identify the specific file with the problem
    import re as _re2
    _err_file_match = _re2.search(r'tests/([\w]+\.py)', error_details)
    if _err_file_match:
        errored_file = tests_dir / _err_file_match.group(1)
        test_path = errored_file if errored_file.exists() else tests_dir / "test_connector.py"
    else:
        test_path = tests_dir / "test_connector.py"

    if not test_path.exists():
        # Fall back to any test file that exists
        all_test_files_found = sorted(tests_dir.glob("test_*.py"))
        if not all_test_files_found:
            await _emit(log_cb, "error", "No test files found — cannot fix")
            return {"status": "fail", "output": "No test files found"}
        test_path = all_test_files_found[0]

    await _emit(log_cb, "info", f"Fixing {test_path.name} (identified from error context)...")

    # ── Auto-fix conftest.py: remove invalid `self` parameter from module-level fixtures ──
    # Gemini sometimes writes `def setup_mocks(self, connector):` at module level —
    # `self` is only valid inside a class body, not in a module-level pytest fixture.
    # These fixtures also reference self.mock_* which don't exist outside a class.
    # Fix: remove `self` from the parameter list of any module-level @pytest.fixture.
    conftest_path = tests_dir / "conftest.py"
    if conftest_path.exists():
        try:
            _conf_src = conftest_path.read_text(encoding="utf-8")
            _conf_tree = ast.parse(_conf_src)
            _conf_lines = _conf_src.splitlines()
            _conf_changed = False
            _lines_to_delete: set = set()
            _deleted_fixtures: list = []
            for _node in ast.iter_child_nodes(_conf_tree):
                if not isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # Check if it has @pytest.fixture decorator
                _has_fixture = any(
                    (isinstance(_d, ast.Attribute) and _d.attr == "fixture") or
                    (isinstance(_d, ast.Call) and isinstance(_d.func, ast.Attribute) and _d.func.attr == "fixture") or
                    (isinstance(_d, ast.Name) and _d.id == "fixture")
                    for _d in _node.decorator_list
                )
                if not _has_fixture:
                    continue
                # Check if first argument is `self` OR body uses self.x — class-method
                # written at module level. Just removing `self` from params leaves NameErrors.
                # Best fix: DELETE the entire fixture so Gemini rewrites TestSync properly.
                _args = _node.args.args
                _has_self_param2 = _args and _args[0].arg == "self"
                _has_self_body2 = any(
                    isinstance(_sn2, ast.Attribute) and
                    isinstance(_sn2.value, ast.Name) and _sn2.value.id == "self"
                    for _sn2 in ast.walk(_node)
                )
                if _has_self_param2 or _has_self_body2:
                    # Mark all lines of this function for deletion (decorator lines + body)
                    # AST gives decorator line numbers in _node.decorator_list[].lineno
                    _start_line = min(
                        (d.lineno for d in _node.decorator_list),
                        default=_node.lineno
                    ) - 1  # 0-indexed
                    _end_line = _node.end_lineno  # 1-indexed inclusive
                    _lines_to_delete.update(range(_start_line, _end_line))
                    _deleted_fixtures.append(_node.name)
                    _conf_changed = True
                    await _emit(log_cb, "info",
                        f"Auto-fix conftest: deleting `{_node.name}` — has invalid `self` param + self.* body (rewrites needed)")
            if _conf_changed and _lines_to_delete:
                _conf_new_lines = [l for i, l in enumerate(_conf_lines) if i not in _lines_to_delete]
                _conf_new_src = "\n".join(_conf_new_lines)
                try:
                    ast.parse(_conf_new_src)
                    conftest_path.write_text(_conf_new_src, encoding="utf-8")
                    await _emit(log_cb, "success",
                        f"✅ conftest.py fixed: deleted invalid self-fixtures {_deleted_fixtures}")
                    # Append to error_details so Gemini knows to rewrite TestSync
                    _self_fixture_hint = (
                        f"\n\n## CRITICAL conftest.py AUTO-FIX APPLIED\n"
                        f"The following fixtures were deleted from conftest.py because they used "
                        f"`self` as first parameter at module level, which is invalid: {_deleted_fixtures}\n"
                        f"You MUST rewrite the affected test classes WITHOUT `self.mock_*` attributes.\n"
                        f"Use `@patch('connector.X')` decorators on each test method instead."
                    )
                    error_details = (error_details or "") + _self_fixture_hint
                except SyntaxError:
                    pass  # Don't write if still broken
        except Exception as _conf_exc:
            await _emit(log_cb, "warn", f"conftest.py auto-fix skipped: {_conf_exc}")

    full_test_code = test_path.read_text(encoding="utf-8")
    connector_code = connector_path.read_text(encoding="utf-8") if connector_path.exists() else ""

    # ── Auto-fix class name mismatches before sending to Gemini ──
    # Extract actual class name from connector.py and replace wrong names in test file
    if connector_code:
        _class_match = _re2.search(r'^class\s+(\w+)\s*\(', connector_code, _re2.MULTILINE)
        if _class_match:
            actual_class = _class_match.group(1)
            # Find what the test file is importing
            _import_match = _re2.search(r'from connector import (\w+)', full_test_code)
            if _import_match:
                wrong_class = _import_match.group(1)
                if wrong_class != actual_class:
                    await _emit(log_cb, "info", f"Auto-fixing class name: '{wrong_class}' → '{actual_class}' in {test_path.name}")
                    full_test_code = full_test_code.replace(wrong_class, actual_class)
                    test_path.write_text(full_test_code, encoding="utf-8")
                    # Verify syntax after auto-fix
                    try:
                        ast.parse(full_test_code)
                        await _emit(log_cb, "success", f"✅ Class name fixed — {test_path.name} is now valid")
                        return {"status": "pass", "output": f"Auto-fixed class name: {wrong_class} → {actual_class}"}
                    except SyntaxError:
                        pass  # Still has issues — fall through to Gemini fix

    # ── Auto-fix -1: strip hallucinated imports BEFORE sending to Gemini ────────────────────
    # Gemini copies whatever bad names it sees in the prompt back into the fix.
    # Stripping them pre-Gemini prevents the cycle from perpetuating.
    _stripped_pre = _strip_hallucinated_imports(full_test_code, connector_path)
    if _stripped_pre != full_test_code:
        try:
            ast.parse(_stripped_pre)
            test_path.write_text(_stripped_pre, encoding="utf-8")
            full_test_code = _stripped_pre
            await _emit(log_cb, "success",
                f"✅ Pre-fix: stripped hallucinated import names from {test_path.name}")
        except SyntaxError:
            pass  # Don't apply if it breaks syntax

    # ── Auto-fix 0: insert `pass` into empty function/class bodies (IndentationError) ──
    # Gemini sometimes generates `async def test_foo(self):\n@decorator` with no body.
    # Fix deterministically by inserting `pass` before falling through to Gemini.
    try:
        ast.parse(full_test_code)
    except SyntaxError as _se0:
        if "expected an indented block" in (_se0.msg or "").lower() or "indentation" in (_se0.msg or "").lower():
            try:
                _lines0 = full_test_code.splitlines()
                _inserted0 = False
                _i0 = 0
                while _i0 < len(_lines0):
                    _line0 = _lines0[_i0]
                    if _re2.match(r'^(\s*)(async\s+)?def\s+\w+.*:\s*$', _line0) or _re2.match(r'^(\s*)class\s+\w+.*:\s*$', _line0):
                        _indent_m0 = _re2.match(r'^(\s*)', _line0)
                        _base_indent0 = _indent_m0.group(1) if _indent_m0 else ""
                        _body_indent0 = _base_indent0 + "    "
                        _next0 = _i0 + 1
                        while _next0 < len(_lines0) and not _lines0[_next0].strip():
                            _next0 += 1
                        if _next0 >= len(_lines0) or not _lines0[_next0].startswith(_body_indent0):
                            _lines0.insert(_i0 + 1, f"{_body_indent0}pass")
                            _inserted0 = True
                    _i0 += 1
                if _inserted0:
                    _fixed0 = "\n".join(_lines0)
                    ast.parse(_fixed0)  # validate fix
                    full_test_code = _fixed0
                    test_path.write_text(full_test_code, encoding="utf-8")
                    await _emit(log_cb, "success",
                        f"✅ Auto-fix 0: inserted `pass` into empty function bodies in {test_path.name}")
            except Exception as _exc0:
                await _emit(log_cb, "warn", f"Auto-fix 0 (empty body) failed: {_exc0}")

    # ── Auto-fix A+B: class fixture patterns (run both in one pass before Gemini) ──
    # A: remove @pytest.fixture applied to a ClassDef (e.g. @pytest.fixture\nclass TestFoo:)
    # B: move @pytest.fixture from inside a class body (method) to module level
    # Both raise ValueError("class fixtures not supported") at collection time.
    # We run A then B on the same in-memory code, then write once if anything changed.
    _autofix_ab_changed = False
    try:
        _tree2 = ast.parse(full_test_code)
        _lines2 = full_test_code.splitlines()
        _class_fixture_lines_to_remove: list = []

        for _top in ast.iter_child_nodes(_tree2):
            if not isinstance(_top, ast.ClassDef):
                continue
            for _dec in _top.decorator_list:
                _is_fix2 = (
                    (isinstance(_dec, ast.Attribute) and _dec.attr == "fixture") or
                    (isinstance(_dec, ast.Name) and _dec.id == "fixture") or
                    (isinstance(_dec, ast.Call) and (
                        (isinstance(_dec.func, ast.Attribute) and _dec.func.attr == "fixture") or
                        (isinstance(_dec.func, ast.Name) and _dec.func.id == "fixture")
                    ))
                )
                if _is_fix2:
                    # Record the line of this decorator (0-indexed)
                    _class_fixture_lines_to_remove.append(_dec.lineno - 1)

        if _class_fixture_lines_to_remove:
            await _emit(log_cb, "info",
                f"Auto-fix A: removing {len(_class_fixture_lines_to_remove)} @pytest.fixture decorator(s) from test class definition(s)...")
            _new_lines2 = [l for i, l in enumerate(_lines2) if i not in set(_class_fixture_lines_to_remove)]
            _fixed_code2 = "\n".join(_new_lines2)
            try:
                ast.parse(_fixed_code2)
                # Update in-memory code so fix-B below also operates on the updated code.
                # Do NOT write to disk yet — B may also have fixes to apply.
                full_test_code = _fixed_code2
                _autofix_ab_changed = True
                await _emit(log_cb, "success",
                    f"✅ Auto-fix A: removed @pytest.fixture from test class(es) in {test_path.name}")
            except SyntaxError as _se2:
                await _emit(log_cb, "warn", f"Auto-fix A produced syntax error ({_se2}) — continuing")
    except Exception as _fix_exc2:
        await _emit(log_cb, "warn", f"Class decorator auto-fix check failed ({_fix_exc2})")

    # ── Auto-fix: move @pytest.fixture decorators from function-inside-class body to module level ──
    # pytest raises ValueError("class fixtures not supported") when @pytest.fixture
    # appears inside a class. This is a deterministic fix — no LLM needed.
    try:
        _tree = ast.parse(full_test_code)
        _lines = full_test_code.splitlines()
        _fixture_patches: list = []  # (class_start_line, fixture_start_line, fixture_end_line)

        for _node in ast.walk(_tree):
            if not isinstance(_node, ast.ClassDef):
                continue
            for _child in list(_node.body):
                if not isinstance(_child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # Check if any decorator is @pytest.fixture
                for _dec in _child.decorator_list:
                    _is_fixture = (
                        (isinstance(_dec, ast.Attribute) and _dec.attr == "fixture") or
                        (isinstance(_dec, ast.Name) and _dec.id == "fixture") or
                        (isinstance(_dec, ast.Call) and (
                            (isinstance(_dec.func, ast.Attribute) and _dec.func.attr == "fixture") or
                            (isinstance(_dec.func, ast.Name) and _dec.func.id == "fixture")
                        ))
                    )
                    if _is_fixture:
                        # Collect: class node's start line, and fixture function's full span
                        _dec_start = _dec.lineno - 1  # 0-indexed, decorator start
                        _func_end = getattr(_child, "end_lineno", _child.lineno)  # 1-indexed
                        _fixture_patches.append((_node.lineno, _child.lineno, _dec_start, _func_end, _node, _child))
                        break

        if _fixture_patches:
            await _emit(log_cb, "info",
                f"Auto-fix: moving {len(_fixture_patches)} class-level @pytest.fixture(s) to module level...")
            # Rebuild file: extract fixture functions from inside classes, inject before the class
            # Process patches in reverse order (by class line) to preserve offsets
            _new_lines = list(_lines)

            # Collect all (class_lineno, fixture_extract_lines, fixture_class_line_range)
            _extractions = []
            for (_class_lineno, _func_lineno, _dec_start_0, _func_end_1, _cls_node, _fn_node) in _fixture_patches:
                # Lines of the entire fixture (decorator + def + body), 0-indexed
                _start_0 = _dec_start_0
                _end_0 = _func_end_1  # exclusive, 0-indexed equivalent: _func_end_1 (since 1-indexed end)
                # Get the indented fixture code
                _fixture_lines_raw = _lines[_start_0:_end_0]
                # Detect indentation (should be 4 spaces inside class)
                _indent = ""
                for _l in _fixture_lines_raw:
                    if _l.strip():
                        _indent = len(_l) - len(_l.lstrip())
                        _indent = " " * _indent
                        break
                # Remove one level of indentation (4 spaces usually)
                _dedented = []
                for _l in _fixture_lines_raw:
                    if _l.startswith("    "):
                        _dedented.append(_l[4:])
                    else:
                        _dedented.append(_l)
                _extractions.append({
                    "class_lineno_0": _class_lineno - 1,  # 0-indexed class line
                    "fixture_start_0": _start_0,
                    "fixture_end_0": _end_0,
                    "fixture_lines": _dedented,
                })

            # Collect module-level fixture names that already exist to avoid duplicates
            _existing_module_fixtures: set = set()
            for _top_node in ast.iter_child_nodes(_tree):
                if isinstance(_top_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for _top_dec in _top_node.decorator_list:
                        _is_fix = (
                            (isinstance(_top_dec, ast.Attribute) and _top_dec.attr == "fixture") or
                            (isinstance(_top_dec, ast.Name) and _top_dec.id == "fixture") or
                            (isinstance(_top_dec, ast.Call) and (
                                (isinstance(_top_dec.func, ast.Attribute) and _top_dec.func.attr == "fixture") or
                                (isinstance(_top_dec.func, ast.Name) and _top_dec.func.id == "fixture")
                            ))
                        )
                        if _is_fix:
                            _existing_module_fixtures.add(_top_node.name)

            # Apply in reverse order of fixture start so line numbers stay valid
            for _ext in sorted(_extractions, key=lambda x: x["fixture_start_0"], reverse=True):
                # Get fixture function name from its lines
                _fn_name = None
                for _l in _ext["fixture_lines"]:
                    _m = _re2.match(r'\s*(?:async\s+)?def\s+(\w+)', _l)
                    if _m:
                        _fn_name = _m.group(1)
                        break

                # Remove fixture from class body
                del _new_lines[_ext["fixture_start_0"]:_ext["fixture_end_0"]]

                # Only insert at module level if no fixture with same name already exists there
                if _fn_name and _fn_name in _existing_module_fixtures:
                    await _emit(log_cb, "info",
                        f"  Skipping insert of '{_fn_name}' — module-level fixture already exists")
                else:
                    # Insert before the class (at class_lineno - offset due to deletion)
                    _insert_at = _ext["class_lineno_0"]
                    _insert_at = min(_insert_at, len(_new_lines))
                    _new_lines[_insert_at:_insert_at] = _ext["fixture_lines"] + [""]
                    if _fn_name:
                        _existing_module_fixtures.add(_fn_name)

            _fixed_code = "\n".join(_new_lines)
            try:
                ast.parse(_fixed_code)
                test_path.write_text(_fixed_code, encoding="utf-8")
                full_test_code = _fixed_code
                _autofix_ab_changed = True
                await _emit(log_cb, "success",
                    f"✅ Auto-fix B: moved {len(_fixture_patches)} class-level fixture(s) to module level in {test_path.name}")
                # Return immediately — A+B both ran; file is now valid
                return {"status": "pass", "output": f"Auto-fixed {len(_fixture_patches)} class-level fixture(s) moved to module level"}
            except SyntaxError as _se:
                await _emit(log_cb, "warn",
                    f"Auto-fix B produced syntax error ({_se}) — falling through to Gemini fix")
                # Revert: restore original
                test_path.write_text(full_test_code, encoding="utf-8")
                _autofix_ab_changed = False
    except Exception as _fix_exc:
        await _emit(log_cb, "warn", f"Class fixture auto-fix failed ({_fix_exc}) — falling through to Gemini")

    # If auto-fix A changed the code but B had nothing to move, write A's fixes and return early.
    if _autofix_ab_changed:
        try:
            ast.parse(full_test_code)
            test_path.write_text(full_test_code, encoding="utf-8")
            await _emit(log_cb, "success", f"✅ Auto-fix A+B complete — {test_path.name} written")
            return {"status": "pass", "output": "Auto-fixed class fixture decorator(s) from class definitions"}
        except SyntaxError as _final_se:
            await _emit(log_cb, "warn", f"Post auto-fix syntax error ({_final_se}) — falling through to Gemini")

    # ── Auto-fix C: duplicate @pytest.fixture on the same function ──────────────────────────
    # Error: "ValueError: @pytest.fixture is being applied more than once to the same function"
    # Cause: LLM emits @pytest.fixture twice on the same def (e.g. after a refactor / merge).
    # Fix:   keep only the FIRST @pytest.fixture decorator on each function, drop the rest.
    try:
        _tree_c = ast.parse(full_test_code)
        _lines_c = full_test_code.splitlines()
        _dup_decorator_lines_to_remove: list = []  # 0-indexed line numbers to drop

        def _is_fixture_decorator(node: ast.expr) -> bool:
            return (
                (isinstance(node, ast.Name) and node.id == "fixture") or
                (isinstance(node, ast.Attribute) and node.attr == "fixture") or
                (isinstance(node, ast.Call) and (
                    (isinstance(node.func, ast.Name) and node.func.id == "fixture") or
                    (isinstance(node.func, ast.Attribute) and node.func.attr == "fixture")
                ))
            )

        for _fn_c in ast.walk(_tree_c):
            if not isinstance(_fn_c, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            _fixture_decs = [d for d in _fn_c.decorator_list if _is_fixture_decorator(d)]
            if len(_fixture_decs) > 1:
                # Keep the first, drop all subsequent ones
                for _extra_dec in _fixture_decs[1:]:
                    _dup_decorator_lines_to_remove.append(_extra_dec.lineno - 1)  # 0-indexed

        if _dup_decorator_lines_to_remove:
            await _emit(log_cb, "info",
                f"Auto-fix C: removing {len(_dup_decorator_lines_to_remove)} duplicate @pytest.fixture decorator(s)...")
            _lines_c_new = [l for i, l in enumerate(_lines_c) if i not in set(_dup_decorator_lines_to_remove)]
            _fixed_c = "\n".join(_lines_c_new)
            try:
                ast.parse(_fixed_c)
                test_path.write_text(_fixed_c, encoding="utf-8")
                full_test_code = _fixed_c
                await _emit(log_cb, "success",
                    f"✅ Auto-fix C: removed duplicate @pytest.fixture from {len(_dup_decorator_lines_to_remove)} function(s) in {test_path.name}")
                return {"status": "pass", "output": f"Auto-fixed duplicate @pytest.fixture decorator(s)"}
            except SyntaxError as _se_c:
                await _emit(log_cb, "warn",
                    f"Auto-fix C produced syntax error ({_se_c}) — falling through to Gemini")
                test_path.write_text(full_test_code, encoding="utf-8")
    except Exception as _fix_exc_c:
        await _emit(log_cb, "warn", f"Duplicate fixture auto-fix check failed ({_fix_exc_c}) — falling through to Gemini")

    # ── Auto-fix D: missing `import pytest` ─────────────────────────────────────────────────
    # Error: "NameError: name 'pytest' is not defined" or "name 'fixture' is not defined"
    # Cause: LLM writes @pytest.fixture / pytest.mark.* but forgets the import at the top.
    # Fix:   prepend `import pytest` if the file uses pytest symbols but doesn't import it.
    try:
        _has_pytest_usage = (
            "@pytest." in full_test_code or
            "pytest.mark." in full_test_code or
            "pytest.raises" in full_test_code or
            "pytest.fixture" in full_test_code or
            "@fixture" in full_test_code
        )
        _has_pytest_import = bool(_re2.search(r'^\s*import pytest\b', full_test_code, _re2.MULTILINE))
        if _has_pytest_usage and not _has_pytest_import:
            _lines_d = full_test_code.splitlines()
            # Insert after any __future__ imports or at very top
            _insert_at_d = 0
            for _i_d, _l_d in enumerate(_lines_d):
                if _l_d.startswith("from __future__") or _l_d.startswith("# -*-"):
                    _insert_at_d = _i_d + 1
                elif _l_d.strip() and not _l_d.startswith("#"):
                    break
            _lines_d.insert(_insert_at_d, "import pytest")
            _fixed_d = "\n".join(_lines_d)
            try:
                ast.parse(_fixed_d)
                test_path.write_text(_fixed_d, encoding="utf-8")
                full_test_code = _fixed_d
                await _emit(log_cb, "success", f"✅ Auto-fix D: added missing `import pytest` to {test_path.name}")
            except SyntaxError:
                pass  # don't apply broken fix
    except Exception as _fix_exc_d:
        await _emit(log_cb, "warn", f"Auto-fix D (missing import pytest) failed: {_fix_exc_d}")

    # ── Auto-fix E: async test functions missing @pytest.mark.asyncio ───────────────────────
    # Error: "PytestUnraisableExceptionWarning: coroutine 'test_X' was never awaited" or
    #        "RuntimeWarning: Enable asyncio mode" or tests silently skipped.
    # Cause: LLM writes `async def test_*` without the asyncio marker.
    # Fix:   add @pytest.mark.asyncio above every async def test_* that lacks it.
    try:
        _tree_e = ast.parse(full_test_code)
        _lines_e = full_test_code.splitlines()
        _asyncio_inserts: list = []  # (line_index_to_insert_before, indent_str)

        def _has_asyncio_marker(fn_node) -> bool:
            for _d in fn_node.decorator_list:
                if isinstance(_d, ast.Attribute) and _d.attr == "asyncio":
                    return True
                if isinstance(_d, ast.Call):
                    if isinstance(_d.func, ast.Attribute) and _d.func.attr == "asyncio":
                        return True
            return False

        for _fn_e in ast.walk(_tree_e):
            if not isinstance(_fn_e, ast.AsyncFunctionDef):
                continue
            if not _fn_e.name.startswith("test_"):
                continue
            if _has_asyncio_marker(_fn_e):
                continue
            # Find where the first decorator or the def line starts
            _dec_start_e = (
                min(d.lineno for d in _fn_e.decorator_list) - 1
                if _fn_e.decorator_list else _fn_e.lineno - 1
            )
            _indent_e = _re2.match(r'^(\s*)', _lines_e[_dec_start_e]).group(1)
            _asyncio_inserts.append((_dec_start_e, _indent_e))

        if _asyncio_inserts:
            # Insert in reverse order to preserve line offsets
            for _ins_line, _ins_indent in sorted(_asyncio_inserts, reverse=True):
                _lines_e.insert(_ins_line, f"{_ins_indent}@pytest.mark.asyncio")
            _fixed_e = "\n".join(_lines_e)
            try:
                ast.parse(_fixed_e)
                test_path.write_text(_fixed_e, encoding="utf-8")
                full_test_code = _fixed_e
                await _emit(log_cb, "success",
                    f"✅ Auto-fix E: added @pytest.mark.asyncio to {len(_asyncio_inserts)} async test(s) in {test_path.name}")
            except SyntaxError:
                pass
    except Exception as _fix_exc_e:
        await _emit(log_cb, "warn", f"Auto-fix E (missing asyncio marker) failed: {_fix_exc_e}")

    # ── Auto-fix F: duplicate test function names ────────────────────────────────────────────
    # Error: pytest silently overrides the first test, only runs the last; or
    #        "ValueError: duplicate 'test_X'" in some pytest versions.
    # Cause: LLM regenerates a test file and emits the same test_ name twice.
    # Fix:   append _2, _3 suffix to every duplicate test function name.
    try:
        _tree_f = ast.parse(full_test_code)
        _lines_f = full_test_code.splitlines()
        _seen_test_names: dict = {}  # name -> count
        _renames_f: list = []  # (lineno_0indexed, old_name, new_name)

        for _fn_f in ast.walk(_tree_f):
            if not isinstance(_fn_f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _fn_f.name.startswith("test_"):
                continue
            _count_f = _seen_test_names.get(_fn_f.name, 0)
            _seen_test_names[_fn_f.name] = _count_f + 1
            if _count_f > 0:
                _new_name_f = f"{_fn_f.name}_{_count_f + 1}"
                _renames_f.append((_fn_f.lineno - 1, _fn_f.name, _new_name_f))

        if _renames_f:
            for _ln_f, _old_f, _new_f in _renames_f:
                _lines_f[_ln_f] = _lines_f[_ln_f].replace(f"def {_old_f}(", f"def {_new_f}(", 1)
            _fixed_f = "\n".join(_lines_f)
            try:
                ast.parse(_fixed_f)
                test_path.write_text(_fixed_f, encoding="utf-8")
                full_test_code = _fixed_f
                await _emit(log_cb, "success",
                    f"✅ Auto-fix F: renamed {len(_renames_f)} duplicate test function(s) in {test_path.name}")
            except SyntaxError:
                pass
    except Exception as _fix_exc_f:
        await _emit(log_cb, "warn", f"Auto-fix F (duplicate test names) failed: {_fix_exc_f}")

    # ── Auto-fix G: tabs mixed with spaces (TabError) ────────────────────────────────────────
    # Error: "TabError: inconsistent use of tabs and spaces in indentation"
    # Cause: LLM outputs a mix of tab (\t) and space indentation.
    # Fix:   replace every leading tab with 4 spaces throughout the file.
    try:
        if "\t" in full_test_code:
            _lines_g = full_test_code.splitlines()
            _fixed_g_lines = []
            for _l_g in _lines_g:
                _leading_g = len(_l_g) - len(_l_g.lstrip("\t"))
                if _leading_g:
                    _l_g = ("    " * _leading_g) + _l_g.lstrip("\t")
                _fixed_g_lines.append(_l_g)
            _fixed_g = "\n".join(_fixed_g_lines)
            try:
                ast.parse(_fixed_g)
                test_path.write_text(_fixed_g, encoding="utf-8")
                full_test_code = _fixed_g
                await _emit(log_cb, "success", f"✅ Auto-fix G: converted tabs to spaces in {test_path.name}")
            except SyntaxError:
                pass
    except Exception as _fix_exc_g:
        await _emit(log_cb, "warn", f"Auto-fix G (tabs) failed: {_fix_exc_g}")

    # ── Auto-fix H: wrong connector import path ──────────────────────────────────────────────
    # Error: "ModuleNotFoundError: No module named 'gmail_connector'" or
    #        "ImportError: attempted relative import with no known parent package"
    # Cause: LLM uses `from gmail_connector.connector import X` or `from . import connector`
    #        instead of the correct `from connector import X`.
    # Fix:   rewrite all non-standard connector import lines to `from connector import X`.
    try:
        _lines_h = full_test_code.splitlines()
        _changed_h = False
        _fixed_h_lines = []
        for _l_h in _lines_h:
            # Match: `from <pkg>.connector import X` or `from .<pkg> import connector`
            _m_h1 = _re2.match(r'^(\s*)from\s+\w+\.connector\s+import\s+(.+)$', _l_h)
            _m_h2 = _re2.match(r'^(\s*)from\s+\.\w*\s+import\s+connector\b', _l_h)
            _m_h3 = _re2.match(r'^(\s*)import\s+\w+\.connector\b', _l_h)
            if _m_h1:
                _fixed_h_lines.append(f"{_m_h1.group(1)}from connector import {_m_h1.group(2)}")
                _changed_h = True
            elif _m_h2:
                _fixed_h_lines.append(f"from connector import connector")
                _changed_h = True
            elif _m_h3:
                _fixed_h_lines.append(f"# import removed: {_l_h.strip()} — use `from connector import X` instead")
                _changed_h = True
            else:
                _fixed_h_lines.append(_l_h)
        if _changed_h:
            _fixed_h = "\n".join(_fixed_h_lines)
            try:
                ast.parse(_fixed_h)
                test_path.write_text(_fixed_h, encoding="utf-8")
                full_test_code = _fixed_h
                await _emit(log_cb, "success", f"✅ Auto-fix H: corrected connector import path(s) in {test_path.name}")
            except SyntaxError:
                pass
    except Exception as _fix_exc_h:
        await _emit(log_cb, "warn", f"Auto-fix H (import path) failed: {_fix_exc_h}")

    # ── Auto-fix I: missing tests/__init__.py ────────────────────────────────────────────────
    # Error: "ImportError: cannot import name 'X' from 'tests'" or pytest collection fails
    #        silently when tests/ lacks __init__.py in some project layouts.
    # Fix:   create an empty tests/__init__.py if it doesn't exist.
    try:
        _init_path = tests_dir / "__init__.py"
        if not _init_path.exists():
            _init_path.write_text("", encoding="utf-8")
            await _emit(log_cb, "info", f"Auto-fix I: created empty tests/__init__.py")
    except Exception as _fix_exc_i:
        await _emit(log_cb, "warn", f"Auto-fix I (__init__.py) failed: {_fix_exc_i}")

    # ── Auto-fix J: @pytest.fixture with invalid scope value ────────────────────────────────
    # Error: "pytest.FixtureScopeError" or "ValueError: scope '<x>' is not allowed"
    # Allowed scopes: "function", "class", "module", "package", "session"
    # Cause: LLM emits scope="request" / scope="test" / scope="global" / typos.
    # Fix:   replace invalid scope strings with "function" (safest default).
    try:
        _VALID_SCOPES = {"function", "class", "module", "package", "session"}
        _lines_j = full_test_code.splitlines()
        _changed_j = False
        _fixed_j_lines = []
        for _l_j in _lines_j:
            _m_j = _re2.search(r'@pytest\.fixture\s*\(.*scope\s*=\s*["\'](\w+)["\']', _l_j)
            if _m_j and _m_j.group(1) not in _VALID_SCOPES:
                _bad_scope = _m_j.group(1)
                _fixed_j_lines.append(_l_j.replace(f'scope="{_bad_scope}"', 'scope="function"')
                                         .replace(f"scope='{_bad_scope}'", 'scope="function"'))
                _changed_j = True
                await _emit(log_cb, "info", f"Auto-fix J: replaced invalid scope='{_bad_scope}' with scope='function'")
            else:
                _fixed_j_lines.append(_l_j)
        if _changed_j:
            _fixed_j = "\n".join(_fixed_j_lines)
            try:
                ast.parse(_fixed_j)
                test_path.write_text(_fixed_j, encoding="utf-8")
                full_test_code = _fixed_j
                await _emit(log_cb, "success", f"✅ Auto-fix J: fixed invalid fixture scope(s) in {test_path.name}")
            except SyntaxError:
                pass
    except Exception as _fix_exc_j:
        await _emit(log_cb, "warn", f"Auto-fix J (invalid scope) failed: {_fix_exc_j}")

    # ── Auto-fix K: `return` with value inside a yield-fixture (SyntaxError in generator) ───
    # Error: "SyntaxError: 'return' with argument inside generator"
    # Cause: LLM writes a fixture that uses `yield` for teardown but also has `return value`
    #        instead of just `yield value`. Two patterns: (a) return before yield, (b) return after yield.
    # Fix:   replace `return <expr>` with `yield <expr>` when the function also has a yield.
    try:
        _tree_k = ast.parse(full_test_code)
        _lines_k = full_test_code.splitlines()
        _changed_k = False

        for _fn_k in ast.walk(_tree_k):
            if not isinstance(_fn_k, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Check if function has @pytest.fixture decorator
            _is_fixture_k = any(
                (isinstance(_d, ast.Name) and _d.id == "fixture") or
                (isinstance(_d, ast.Attribute) and _d.attr == "fixture") or
                (isinstance(_d, ast.Call) and (
                    (isinstance(_d.func, ast.Name) and _d.func.id == "fixture") or
                    (isinstance(_d.func, ast.Attribute) and _d.func.attr == "fixture")
                ))
                for _d in _fn_k.decorator_list
            )
            if not _is_fixture_k:
                continue
            # Does it contain both yield and return-with-value?
            _has_yield_k = any(isinstance(_n, ast.Expr) and isinstance(_n.value, ast.Yield)
                               for _n in ast.walk(_fn_k))
            _return_nodes_k = [_n for _n in ast.walk(_fn_k)
                               if isinstance(_n, ast.Return) and _n.value is not None]
            if _has_yield_k and _return_nodes_k:
                for _ret_k in _return_nodes_k:
                    _ret_line_k = _ret_k.lineno - 1  # 0-indexed
                    # Replace `return <expr>` → `yield <expr>` on that line
                    _lines_k[_ret_line_k] = _re2.sub(r'\breturn\b', 'yield', _lines_k[_ret_line_k], count=1)
                _changed_k = True

        if _changed_k:
            _fixed_k = "\n".join(_lines_k)
            try:
                ast.parse(_fixed_k)
                test_path.write_text(_fixed_k, encoding="utf-8")
                full_test_code = _fixed_k
                await _emit(log_cb, "success",
                    f"✅ Auto-fix K: replaced `return` with `yield` in generator fixture(s) in {test_path.name}")
            except SyntaxError:
                pass
    except Exception as _fix_exc_k:
        await _emit(log_cb, "warn", f"Auto-fix K (return in generator) failed: {_fix_exc_k}")

    # ── Auto-fix L: test function name doesn't start with `test_` ───────────────────────────
    # Error: pytest collects 0 tests (no error, just nothing runs).
    # Cause: LLM names tests `check_*`, `verify_*`, `it_*`, `should_*` instead of `test_*`.
    # Fix:   rename to `test_<original>` for common non-pytest naming conventions.
    try:
        _tree_l = ast.parse(full_test_code)
        _lines_l = full_test_code.splitlines()
        _NON_TEST_PREFIXES = ("check_", "verify_", "it_", "should_", "spec_", "assert_")
        _renames_l: list = []  # (lineno_0indexed, old_name, new_name)

        for _fn_l in ast.walk(_tree_l):
            if not isinstance(_fn_l, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            _name_l = _fn_l.name
            if any(_name_l.startswith(p) for p in _NON_TEST_PREFIXES):
                _new_name_l = "test_" + _name_l
                _renames_l.append((_fn_l.lineno - 1, _name_l, _new_name_l))

        if _renames_l:
            for _ln_l, _old_l, _new_l in _renames_l:
                _lines_l[_ln_l] = _lines_l[_ln_l].replace(f"def {_old_l}(", f"def {_new_l}(", 1)
            _fixed_l = "\n".join(_lines_l)
            try:
                ast.parse(_fixed_l)
                test_path.write_text(_fixed_l, encoding="utf-8")
                full_test_code = _fixed_l
                await _emit(log_cb, "success",
                    f"✅ Auto-fix L: renamed {len(_renames_l)} non-pytest test function(s) to test_* in {test_path.name}")
            except SyntaxError:
                pass
    except Exception as _fix_exc_l:
        await _emit(log_cb, "warn", f"Auto-fix L (test_ prefix) failed: {_fix_exc_l}")

    full_line_count = len(full_test_code.splitlines())

    # ── Focused fix: only send failing tests, not the entire file ──
    # Large test files (1000+ lines) + connector code cause LLM timeouts.
    # We extract just the imports + failing test classes/functions to keep
    # the prompt under ~400 lines so the LLM can respond in < 10 minutes.
    #
    # Exception: structural errors that require moving code (e.g. @pytest.fixture
    # inside a class → must move to module level) need the FULL FILE so the
    # merge logic doesn't lose the moved fixture.
    _FULL_FILE_REQUIRED_PATTERNS = [
        "class fixtures not supported",
        "valueerror: class",
        "fixture.*not.*supported",
        "applied more than once",
        "duplicate.*fixture",
        "return.*inside generator",
        "tabError",
        "inconsistent use of tabs",
        "no module named",
        "attempted relative import",
        "fixturescope",
    ]
    _err_lower = error_details.lower()
    _force_full_file = any(p in _err_lower for p in _FULL_FILE_REQUIRED_PATTERNS)

    if _force_full_file:
        focused_code = full_test_code
    else:
        focused_code = _focused_test_code(full_test_code, error_details)
    focused_line_count = len(focused_code.splitlines())
    is_focused = (not _force_full_file) and (focused_line_count < full_line_count)

    if is_focused:
        await _emit(log_cb, "info", f"Focused fix: extracted {focused_line_count} lines from {full_line_count} total (targeting failing tests only)...")
    else:
        await _emit(log_cb, "info", "Sending test code + error details to Claude for fix...")

    # Always extract class_name from actual connector.py — context may be stale
    _fix_class_match = re.search(r"^class\s+(\w+)\s*\(BaseConnector\)", connector_code, re.MULTILINE)
    _fix_class_name = _fix_class_match.group(1) if _fix_class_match else context.get("class_name", "Connector")

    # Extract ONLY the public methods defined directly on the connector class body.
    # We exclude BaseConnector internals (get_token, set_token, get_config, save_config, etc.)
    # so that Gemini knows to DELETE test classes targeting those non-existent public methods.
    _fix_valid_methods: list = []
    if connector_code:
        try:
            _fix_tree = ast.parse(connector_code)
            _fix_connector_class = None
            for _n in ast.walk(_fix_tree):
                if isinstance(_n, ast.ClassDef):
                    _base_names = [
                        b.id if isinstance(b, ast.Name) else (b.attr if isinstance(b, ast.Attribute) else "")
                        for b in _n.bases
                    ]
                    if "BaseConnector" in _base_names:
                        _fix_connector_class = _n
                        break
            if _fix_connector_class:
                for _item in _fix_connector_class.body:
                    if isinstance(_item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not _item.name.startswith("_"):
                            _fix_valid_methods.append(_item.name)
        except Exception:
            pass  # AST parse failure — fall back to empty list

    _valid_methods_str = (
        "  - " + "\n  - ".join(_fix_valid_methods)
        if _fix_valid_methods
        else "  (could not extract — treat ALL non-underscore methods in connector.py class body as valid)"
    )
    if _fix_valid_methods:
        await _emit(log_cb, "info", f"Valid connector methods for fix: {', '.join(_fix_valid_methods)}")

    # Load TEST_CASE_WRITING_GUIDELINES.md for the fix path too — R2 → local fallback
    from integration.services.guidelines_service import get_test_case_writing_guidelines
    _fix_guidelines_content = await get_test_case_writing_guidelines()
    _fix_guidelines = ""
    if _fix_guidelines_content:
        _fix_guidelines = (
            "## ══ SHIELVA TEST CASE WRITING GUIDELINES (read before fixing) ══\n"
            + _fix_guidelines_content
            + "\n## ══ END OF GUIDELINES ══\n\n"
        )

    # Load per-service test_rules.md for fix path too
    _fix_service_rules = ""
    _fix_provider = context.get("provider", "").lower().replace(" ", "_")
    _fix_service = context.get("service_name", "").lower().replace(" ", "_")
    if _fix_provider and _fix_service:
        _fix_service_rules_path = (
            Path(__file__).resolve().parent.parent.parent.parent.parent
            / "shielva-integrations"
            / "shielva-integration-plans"
            / _fix_provider
            / _fix_service
            / "shielva-sense"
            / "test_rules.md"
        )
        if _fix_service_rules_path.exists():
            _fix_service_rules = (
                f"## ══ SERVICE-SPECIFIC TEST RULES — {_fix_provider.upper()} / {_fix_service.upper()} ══\n"
                + _fix_service_rules_path.read_text(encoding="utf-8")
                + "\n## ══ END OF SERVICE RULES ══\n\n"
            )

    # ── Gather full connector context for Gemini ─────────────────────────────────────────────
    _fix_provider_str   = context.get("provider", "unknown")
    _fix_service_str    = context.get("service", context.get("service_slug", "unknown"))
    _fix_conn_name      = context.get("connector_name", "") or _fix_service_str
    _fix_auth_type      = context.get("auth_type", "unknown")
    _fix_user_prompt    = context.get("user_prompt", "(not available)")
    _fix_attempt_num    = context.get("fix_attempt", 1)

    # Previous failed fix strategies (avoid repeating the same mistake)
    _fix_prev_attempts  = context.get("previous_fix_summaries", [])
    _fix_prev_str = (
        "\n".join(f"  Attempt {i+1}: {s}" for i, s in enumerate(_fix_prev_attempts))
        if _fix_prev_attempts
        else "  None — this is the first fix attempt."
    )

    # requirements.txt — installed packages Gemini can use for patching/mocking
    _req_path = out_dir / "requirements.txt"
    _installed_pkgs = _req_path.read_text(encoding="utf-8").strip() if _req_path.exists() else "(requirements.txt not found)"

    # connector.json — capabilities, auth config, feature list
    _conn_json_path = out_dir / "connector.json"
    _conn_json_str = "(connector.json not found)"
    if _conn_json_path.exists():
        try:
            import json as _json_ctx
            _conn_json_str = _json_ctx.dumps(_json_ctx.loads(_conn_json_path.read_text(encoding="utf-8")), indent=2)
        except Exception:
            _conn_json_str = _conn_json_path.read_text(encoding="utf-8")

    # BaseConnector interface — what's already inherited so Gemini doesn't redefine it
    _base_iface_path = (
        Path(__file__).resolve().parent.parent.parent
        / "shared" / "base_connector.py"
    )
    _base_iface_str = "(base_connector.py not found)"
    if _base_iface_path.exists():
        try:
            _base_src = _base_iface_path.read_text(encoding="utf-8")
            # Extract only class signatures + docstrings (not full implementation) to save tokens
            _base_tree_ctx = ast.parse(_base_src)
            _base_lines_ctx = _base_src.splitlines()
            _base_sigs = []
            for _bn in ast.walk(_base_tree_ctx):
                if isinstance(_bn, ast.ClassDef) and "Connector" in _bn.name:
                    for _bm in _bn.body:
                        if isinstance(_bm, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            _sig_end = _bm.body[0].lineno - 1 if _bm.body else _bm.lineno
                            _base_sigs.append("\n".join(_base_lines_ctx[_bm.lineno - 1:_sig_end]))
            _base_iface_str = "\n".join(_base_sigs) if _base_sigs else _base_src[:3000]
        except Exception:
            _base_iface_str = _base_iface_path.read_text(encoding="utf-8")[:3000]

    _import_header = (
        _fix_guidelines
        + _fix_service_rules
        + f"## ⚠️ MANDATORY IMPORT — DO NOT CHANGE:\n"
        f"from connector import {_fix_class_name}\n"
        f"from shared.base_connector import BaseConnector, ConnectorStatus, ConnectorHealth, AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus\n"
        f"## ANY other path (google_adsense_connector, client.connector, adsense_connector.connector, etc.) → ImportError → ALL tests fail.\n\n"
    )
    # Use SafeDict so unknown placeholders in R2-cached prompts (e.g. {date}) don't crash
    class _SafeDict(dict):
        def __missing__(self, key): return "{" + key + "}"
    system = _import_header + (await _get_prompt("FIX_TESTS_PROMPT", FIX_TESTS_PROMPT)).format_map(_SafeDict(
        provider=_fix_provider_str,
        service=_fix_service_str,
        connector_name=_fix_conn_name,
        auth_type=_fix_auth_type,
        user_prompt=_fix_user_prompt,
        fix_attempt=_fix_attempt_num,
        previous_fix_summary=_fix_prev_str,
        base_connector_interface=_base_iface_str,
        installed_packages=_installed_pkgs,
        connector_json=_conn_json_str,
        current_test_code=focused_code,
        connector_code=connector_code,
        error_details=error_details,
        valid_connector_methods=_valid_methods_str,
        step_memory_summary=_build_step_memory_summary(context),
    ))
    # Inject RAG knowledge context (uploaded guidelines, SDK docs, prior generated code)
    system = await _inject_rag_context(system, context)

    # Inject session plan steps so Gemini knows the intended behavior of each method.
    _fix_plan_steps = context.get("plan", {}).get("steps", []) if isinstance(context.get("plan"), dict) else []
    if _fix_plan_steps:
        _fix_plan_lines = []
        for _i, _s in enumerate(_fix_plan_steps):
            _stype = _s.get("type", "") if isinstance(_s, dict) else ""
            _desc = (_s.get("description", "") or _s.get("name", "")) if isinstance(_s, dict) else ""
            if _stype or _desc:
                _fix_plan_lines.append(f"  Step {_i + 1} ({_stype}): {_desc}")
        if _fix_plan_lines:
            system += (
                "\n\n## CONNECTOR BUILD PLAN (the specification — what each method must implement)\n"
                + "\n".join(_fix_plan_lines)
            )

    # When we only sent a focused subset, tell the LLM explicitly
    user_msg = (
        "Fix the failing tests and return ONLY the corrected test classes/functions shown above. "
        "Do NOT return the entire test file — return only the fixed portions shown."
        if is_focused else
        "Fix the errors in these tests and return the complete corrected file."
    )

    messages = [{"role": "user", "content": user_msg}]

    try:
        code = await call_llm_fix(messages, system=system, max_tokens=16384,
                                   on_chunk=_make_gemini_progress_cb(log_cb, "Gemini test fix"))
        code = _clean_llm_code_response(code)

        # Phrases Claude CLI emits when blocked by --allowedTools "" and it wanted to write a file
        _APPROVAL_PHRASES = ("please approve", "approve the write", "need permission",
                             "write operation", "write the file", "apply all fixes")
        _is_approval = any(p in (code or "").lower() for p in _APPROVAL_PHRASES)

        # Guard: retry if non-Python or "please approve" style response
        if not code or len(code) < 50 or (not code.lstrip().startswith(_VALID_PYTHON_STARTS)) or _is_approval:
            reason = "approval-request" if _is_approval else "non-code response"
            await _emit(log_cb, "warn", f"LLM returned {reason} — retrying with direct code-output prompt...")
            # On retry: use a minimal system + embed code inline in user message (avoids file-write confusion)
            retry_system = (
                "Output only Python code. No prose. No explanation. No markdown. "
                "First line must start with 'import', 'from', '#', or '\"\"\"'."
            )
            retry_messages = [
                {
                    "role": "user",
                    "content": (
                        f"Here is Python test code:\n\n"
                        f"```python\n{focused_code if is_focused else full_test_code}\n```\n\n"
                        f"Error details:\n{error_details[:2000]}\n\n"
                        f"Fix the structural errors (imports, class names, mock wiring). "
                        f"Output ONLY the complete corrected Python code. "
                        f"Start immediately with the first import or comment."
                    ),
                },
            ]
            code = await call_llm_fix(retry_messages, system=retry_system, max_tokens=16384,
                                       on_chunk=_make_gemini_progress_cb(log_cb, "Gemini test retry"))
            code = _clean_llm_code_response(code)

        if not code or len(code) < 50 or (not code.lstrip().startswith(_VALID_PYTHON_STARTS)):
            await _emit(log_cb, "error", f"LLM returned invalid response for test fix: {code[:120]}")
            return {"status": "fail", "output": f"LLM fix did not return valid Python test code: {code[:200]}"}

        # Validate syntax of the LLM response — do NOT write broken code to disk
        try:
            ast.parse(code)
        except SyntaxError as exc:
            await _emit(log_cb, "error", f"Fixed test code has syntax error ({exc}) — retrying with explicit fix...")
            # Ask Gemini to fix the syntax error it just introduced
            syntax_retry_messages = [
                {
                    "role": "user",
                    "content": (
                        f"This Python code has a syntax error at line {exc.lineno}: {exc.msg}\n\n"
                        f"```python\n{code}\n```\n\n"
                        f"Fix ONLY the syntax error. Output the complete corrected Python file immediately."
                    ),
                },
            ]
            syntax_retry_system = (
                "Output only Python code. No prose. No markdown. "
                "First line must be an import, comment, or docstring."
            )
            code = await call_llm_fix(syntax_retry_messages, system=syntax_retry_system, max_tokens=16384,
                                      on_chunk=_make_gemini_progress_cb(log_cb, "Gemini syntax retry"))
            code = _clean_llm_code_response(code)
            try:
                ast.parse(code)
                await _emit(log_cb, "success", "Syntax error fixed on retry")
            except SyntaxError as exc2:
                await _emit(log_cb, "error", f"Syntax still broken after retry: {exc2} — original file preserved")
                return {"status": "fail", "output": f"Fixed test code has syntax error: {exc2}"}

        # ── Truncation guard: reject responses shorter than 50% of original ──
        # Only applies for full-file rewrites — focused fixes return a subset by design,
        # so comparing against the full file length would always trigger a false positive.
        if not is_focused:
            original_line_count = len(full_test_code.splitlines())
            fixed_line_count = len(code.splitlines())
            if original_line_count > 50 and fixed_line_count < original_line_count * 0.5:
                await _emit(log_cb, "error",
                            f"Gemini response looks truncated ({fixed_line_count} lines vs {original_line_count} original) — "
                            f"preserving original file")
                return {"status": "fail", "output": f"LLM returned truncated test file ({fixed_line_count} vs {original_line_count} lines)"}

        # ── Post-process: fix connector import path + strip hallucinated names ────
        code = _fix_connector_import(code, _fix_class_name)
        code = _strip_hallucinated_imports(code, out_dir / "connector.py")

        if is_focused:
            # Merge the LLM's fixed functions back into the full test file.
            # Strategy: replace each top-level node by name if it appears in the LLM output.
            try:
                fixed_tree = ast.parse(code)
                original_lines = full_test_code.splitlines()

                # Build a name→new_source map from the LLM output
                fixed_nodes: Dict[str, str] = {}
                code_lines = code.splitlines()
                for node in ast.iter_child_nodes(fixed_tree):
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        start = node.lineno - 1
                        end = getattr(node, "end_lineno", node.lineno)
                        fixed_nodes[node.name] = "\n".join(code_lines[start:end])

                if fixed_nodes:
                    # Replace matching nodes in the original file
                    orig_tree = ast.parse(full_test_code)
                    # Collect (start_line, end_line, replacement) patches — apply in reverse
                    patches = []
                    for node in ast.iter_child_nodes(orig_tree):
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                            if node.name in fixed_nodes:
                                start = node.lineno - 1
                                end = getattr(node, "end_lineno", node.lineno)
                                patches.append((start, end, fixed_nodes[node.name]))

                    # Apply patches in reverse order to preserve line numbers
                    for start, end, replacement in sorted(patches, key=lambda x: x[0], reverse=True):
                        original_lines[start:end] = replacement.splitlines()

                    merged_code = "\n".join(original_lines)
                    # Validate merged result
                    ast.parse(merged_code)
                    test_path.write_text(merged_code, encoding="utf-8")
                    await _emit(log_cb, "success", f"Merged {len(fixed_nodes)} fixed test block(s) into {test_path.name}")
                else:
                    # LLM returned something but no parseable nodes — write focused fix as-is
                    test_path.write_text(code, encoding="utf-8")
            except Exception as merge_exc:
                # Merge failed — fall back to writing full LLM output
                await _emit(log_cb, "warn", f"Merge failed ({merge_exc}) — writing LLM output directly")
                test_path.write_text(code, encoding="utf-8")
        else:
            test_path.write_text(code, encoding="utf-8")

        # Run deterministic auto-fix on the written test file to catch any
        # class-fixture patterns or empty function bodies Gemini re-introduced.
        try:
            from integration.services.code_quality import auto_fix_python_file as _aqf
            _aqf_result = _aqf(test_path)
            if _aqf_result.get("tools_applied"):
                await _emit(log_cb, "success", f"Post-fix auto-fix applied: {_aqf_result['tools_applied']}")
            elif not _aqf_result.get("clean", True):
                # File still has syntax errors after auto-fix — log but continue
                await _emit(log_cb, "warn",
                    f"Post-fix: file still has syntax errors: {_aqf_result.get('syntax_error', 'unknown')}")
        except Exception:
            pass  # never block on auto-fix failure

        quality = analyze_file(str(test_path))

        await _emit(log_cb, "success", f"Fixed tests written → {test_path.name} ({quality.get('line_count', 0)} lines)")
        return {
            "status": "pass",
            "output": {
                "file": str(test_path),
                "line_count": quality.get("line_count", 0),
                "fixed": True,
                "focused_fix": is_focused,
            },
        }
    except Exception as exc:
        await _emit(log_cb, "error", f"Test fix failed: {exc}")
        return {"status": "fail", "output": str(exc)}


# ── Syntax Check & Auto-Fix ───────────────────────────────────────────

async def handle_syntax_check(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback,
) -> Dict[str, Any]:
    """Check syntax of ALL generated Python files (connector + tests) and auto-fix with Gemini.

    Reads every .py file in the connector package including tests/, runs ast.parse()
    on each, and calls Gemini to fix any files with syntax errors.  Loops up to
    MAX_FIX_ATTEMPTS times until the entire package is clean.
    """
    MAX_FIX_ATTEMPTS = 5
    _SKIP_DIRS = {"__pycache__", ".mypy_cache", ".git"}

    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    if not out_dir.exists():
        await _emit(log_cb, "error", "Connector directory not found — run write_connector first")
        return {"status": "fail", "output": "Connector directory not found"}

    provider = context.get("provider", "unknown")
    service_name = context.get("service_name", "Unknown Service")
    auth_type = context.get("auth_type", "unknown")

    def _collect_py_files() -> List[Path]:
        files = []
        for path in sorted(out_dir.rglob("*.py")):
            rel = path.relative_to(out_dir)
            parts = rel.parts
            if any(p in _SKIP_DIRS for p in parts):
                continue
            # Include tests/ — syntax errors in test files are just as important
            files.append(path)
        return files

    def _check_syntax(files: List[Path]) -> List[tuple]:
        """Return list of (path, SyntaxError) for files with errors."""
        errors = []
        for path in files:
            try:
                code = path.read_text(encoding="utf-8")
                ast.parse(code)
            except SyntaxError as exc:
                errors.append((path, exc))
            except Exception:
                pass
        return errors

    py_files = _collect_py_files()
    if not py_files:
        await _emit(log_cb, "warn", "No Python files found to check")
        return {"status": "pass", "output": "No Python files to check"}

    await _emit(log_cb, "info", f"🔍 Checking syntax of {len(py_files)} Python file(s)...")

    # ── Pass 0: Non-AI auto-fix FIRST (autoflake → ruff) on every file ──────
    # Resolves unused imports, indentation, trailing commas, and style issues
    # in milliseconds — no LLM call needed for the majority of generation artefacts.
    from integration.services.code_quality import auto_fix_python_file
    await _emit(log_cb, "info", "🔧 Running non-AI auto-fix pass (autoflake → ruff) on all files...")
    _auto_fixed_count = 0
    for _py_file in py_files:
        _r = auto_fix_python_file(_py_file)
        if _r["tools_applied"]:
            _auto_fixed_count += 1
    if _auto_fixed_count:
        await _emit(log_cb, "info", f"✔ Non-AI auto-fix applied to {_auto_fixed_count} file(s)")

    # Initial check (after non-AI fixes)
    syntax_errors = _check_syntax(py_files)

    if not syntax_errors:
        await _emit(log_cb, "success", f"✅ All {len(py_files)} file(s) have clean syntax — no fixes needed")
        return {"status": "pass", "output": f"All {len(py_files)} files passed syntax check"}

    # Report initial errors
    for path, exc in syntax_errors:
        rel = path.relative_to(out_dir)
        await _emit(log_cb, "error", f"❌ Syntax error in {rel}: {exc}")

    # Auto-fix loop
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        await _emit(log_cb, "info", f"🔧 Fix attempt {attempt}/{MAX_FIX_ATTEMPTS} — sending {len(syntax_errors)} file(s) to Gemini...")

        for bad_path, exc in syntax_errors:
            rel = bad_path.relative_to(out_dir)
            current_code = bad_path.read_text(encoding="utf-8")

            await _emit(log_cb, "info", f"  Fixing {rel} (line {exc.lineno}: {exc.msg})...")

            is_test_file = "tests" in bad_path.parts
            file_role = "Python test file" if is_test_file else f"{service_name} ({provider}) connector file"
            fix_system = (
                f"You are fixing a Python syntax error in a {file_role}.\n\n"
                f"## CONNECTOR IDENTITY\n"
                f"- Provider: {provider}\n"
                f"- Service: {service_name}\n"
                f"- Connector Name: {context.get('connector_name', service_name)}\n"
                f"- Auth Type: {context.get('auth_type', 'unknown')}\n"
                f"- User Requirement: {context.get('user_prompt', '(not provided)')}\n\n"
                f"## WHAT WAS ALREADY BUILT\n{_build_step_memory_summary(context)}\n\n"
                f"Current code with syntax error:\n```python\n{current_code}\n```\n\n"
                f"Syntax error: {exc.msg} at line {exc.lineno}\n\n"
                "Output ONLY the complete corrected Python code. No prose, no markdown fences. "
                "The first line must be an import, comment, or docstring."
            )
            fix_messages = [{"role": "user", "content": "Fix all syntax errors and return the complete corrected Python file."}]
            # Test files can be large — use higher token limit
            fix_max_tokens = 60000 if is_test_file else 60000

            try:
                fixed_code = await call_llm_fix(fix_messages, system=fix_system, max_tokens=fix_max_tokens,
                                                on_chunk=_make_gemini_progress_cb(log_cb, "Gemini syntax fix"))
                fixed_code = _clean_llm_code_response(fixed_code)

                if not fixed_code or len(fixed_code) < 30 or not fixed_code.lstrip().startswith(_VALID_PYTHON_STARTS):
                    await _emit(log_cb, "warn", f"  ⚠ Gemini returned non-code for {rel} — skipping")
                    continue

                bad_path.write_text(fixed_code, encoding="utf-8")
                await _emit(log_cb, "info", f"  ✓ {rel} rewritten by Gemini")

                # Sync __init__.py if connector.py was fixed
                if bad_path.name == "connector.py":
                    actual_class = _sync_init_with_connector(out_dir, context)
                    if actual_class:
                        await _emit(log_cb, "info", f"  __init__.py synced with class: {actual_class}")

            except Exception as fix_exc:
                await _emit(log_cb, "error", f"  Gemini fix failed for {rel}: {fix_exc}")

        # Re-check all files
        py_files = _collect_py_files()
        syntax_errors = _check_syntax(py_files)

        if not syntax_errors:
            await _emit(log_cb, "success", f"✅ All files clean after fix attempt {attempt}")
            quality_scores = []
            for path in py_files:
                q = analyze_file(str(path))
                quality_scores.append(q.get("quality_score", 0))
            avg_score = round(sum(quality_scores) / len(quality_scores)) if quality_scores else 0
            return {
                "status": "pass",
                "output": {
                    "files_checked": len(py_files),
                    "fix_attempts": attempt,
                    "average_quality_score": avg_score,
                },
            }

        await _emit(log_cb, "warn", f"  Still {len(syntax_errors)} error(s) after fix {attempt}")

    # Still errors after all attempts
    remaining = [str(p.relative_to(out_dir)) for p, _ in syntax_errors]
    await _emit(log_cb, "error", f"❌ {len(remaining)} file(s) still have syntax errors after {MAX_FIX_ATTEMPTS} attempts: {', '.join(remaining)}")
    return {
        "status": "fail",
        "output": f"{len(remaining)} file(s) still have syntax errors: {', '.join(remaining)}",
    }


# ── generate_metadata ─────────────────────────────────────────────────

_METADATA_SYSTEM_PROMPT = """You are a JSON schema generator for Shielva connectors.

## CONNECTOR IDENTITY — read this first
- Provider: {provider}
- Service: {service_name}
- Connector Name: {connector_name}
- Auth Type: {auth_type}
- SDK Package: {sdk_package}
- User Requirement: {user_prompt}
- What was built: {step_memory_summary}

Given a Python connector class, generate a `connector.json` metadata file.

## Output
Return ONLY a valid JSON object — no markdown fences, no prose, no explanation.

## Schema
{{
  "connector_type": "<CONNECTOR_TYPE class attribute value — e.g. shielva_gmail_connector>",
  "name": "<Full product name — MUST follow pattern: 'Shielva {service_name} Connector', e.g. 'Shielva Gmail Connector'>",
  "display_name": "<Short service name only — e.g. 'Gmail', 'Slack', 'Salesforce'>",
  "version": "<version string>",
  "description": "<one sentence description of what this connector does>",
  "auth_type": "<see auth_type mapping below>",
  "install_fields": [
    {{
      "key": "<config dict key accessed in install()>",
      "label": "<human-readable label>",
      "type": "<text|password|textarea|number|boolean|select|json>",
      "required": true|false,
      "placeholder": "<realistic example value>",
      "help": "<one sentence help text>",
      "suggestions": [
        {{"label": "<name>", "value": "<value>", "description": "<what this does>"}}
      ]
    }}
  ],
  "apis": [
    {{
      "id": "<snake_case Python method name — e.g. sync, list_emails, send_email>",
      "name": "<human-readable name>",
      "description": "<what this method does>",
      "method": "<HTTP verb: GET for read-only, POST for state-changing operations>",
      "params": [
        {{
          "name": "<Python parameter name>",
          "type": "<string|integer|boolean|datetime|object|array>",
          "required": true|false,
          "default": null,
          "help": "<help text>"
        }}
      ],
      "returns": "<return type description>"
    }}
  ],
  "painter": {{
    "painter_type": "form",
    "config": {{
      "title": "Connect to <display_name>",
      "submit_label": "Connect",
      "fields": "<user-configurable runtime fields ONLY — INCLUDE: scopes, sync_query, region, filters, language and similar preference fields. EXCLUDE: client_id, client_secret, api_key, redirect_uri, and any field with type=password or whose key contains 'secret','key','credential','_id'. If no user-configurable fields exist, use []>"
    }}
  }}
}}

## Rules
1. `connector_type` = exact value of the CONNECTOR_TYPE class attribute in connector.py
2. `name` = ALWAYS "Shielva {service_name} Connector" (e.g. "Shielva Gmail Connector", "Shielva Slack Connector") — this is the full product name shown in the UI
3. `display_name` = short name of the service only (e.g. "Gmail", "Slack")
4. `install_fields` must include every key read from self.config in install() that the user must provide
   - NEVER include `redirect_uri` in install_fields — it is injected automatically by the gateway at runtime
5. API params use `"name"` (not `"key"`) — the Python parameter name as it appears in the method signature
6. `apis.method` = HTTP verb only (GET / POST / PUT / DELETE / PATCH) — NEVER a Python method name
   - GET for: health_check, list_*, get_*, fetch_*, read-only operations
   - POST for: install, authorize, sync, send_*, create_*, delete_* (trash/soft-delete), update_*
7. Use type="password" for any field whose key contains: secret, key, password, token, credential
8. Set required=true for every field that raises an error when missing
9. `painter.config.fields` = **user-configurable runtime fields only** (e.g. scopes, sync_query, region). NEVER include auth credentials: `client_id`, `client_secret`, `api_key`, `redirect_uri`, or any field with `type="password"` or key containing "secret"/"key"/"credential". If no user-configurable fields, use `[]`.
10. `returns` values: use `"ConnectorStatus"`, `"TokenInfo"`, `"SyncResult"` for standard types; use `"list"`, `"object"`, `"null"`, `"boolean"`, `"string"` for primitives/None.
11. version default "1.0.0"
12. **OAuth2 connectors MUST include `client_id` (type="text", required=true) and `client_secret` (type="password", required=true) in `install_fields`** — credentials come from self.config, NEVER from os.environ in the connector.json
13. **For OAuth scope fields**: include a `"suggestions"` array listing common scope options with real OAuth scope URLs — e.g. `{{"label": "Read Only", "value": "https://www.googleapis.com/auth/gmail.readonly", "description": "..."}}`
    - Scope `"value"` must be the actual OAuth scope URL — NEVER a web UI URL like "https://mail.google.com/" (exception: Gmail full-access scope IS `"https://mail.google.com/"`)
14. **For fields with common presets** (like query filters, sync frequency): add a `"suggestions"` array to help users pick from common values

## Auth Type Reference (read AUTH_TYPE from connector.py class attribute)

| connector.py AUTH_TYPE | connector.json auth_type | Has `authorize` in apis? | install_fields |
|---|---|---|---|
| `api_key` | `"api_key"` | ❌ No | `api_key` (password) |
| `bearer` | `"bearer_token"` | ❌ No | `token` (password) |
| `basic` | `"basic"` | ❌ No | `username` (text) + `password` (password) |
| `hmac` | `"api_key"` | ❌ No | `api_key` (text) + `api_secret` (password) |
| `aws_signature`/`aws_sigv4` | `"api_key"` | ❌ No | `access_key_id` (text) + `secret_access_key` (password) + `region` (text) |
| `oauth2_code` | `"oauth2"` | ✅ YES | `client_id` (text) + `client_secret` (password) + optional `scopes` |
| `oauth2_pkce` | `"oauth2"` | ✅ YES | `client_id` (text) only — NO client_secret + optional `scopes` |
| `oauth2_client_credentials` | `"oauth2"` | ❌ No | `client_id` (text) + `client_secret` (password) |
| `oauth2_password` | `"oauth2"` | ❌ No | `client_id` + `client_secret` + `username` + `password` |
| `oauth2_device` | `"oauth2"` | ❌ No | `client_id` (text) + optional `scopes` |
| `service_account` | `"service_account"` | ❌ No | `service_account_json` (textarea) |
| `jwt` | `"jwt"` | ❌ No | `private_key` (textarea) + `client_email` (text) + `token_uri` (text) |
| `none` | `"none"` | ❌ No | `[]` (empty) |

**Only `oauth2_code` and `oauth2_pkce` have `authorize` in the apis array.** All other auth types are handled automatically by the gateway — do NOT add an `authorize` entry for them.
"""


_SETUP_INSTRUCTIONS_SYSTEM = """You are a technical documentation expert generating connector-specific setup instructions for the Shielva platform.

Your job: produce a clear, step-by-step markdown guide that tells a developer/admin EXACTLY where to go to find the credentials required to configure this connector.

## Your workflow
1. Call `read_file('connector.py')` — understand the connector class, auth_type, and what credentials it needs from `self.config`
2. Call `read_file('metadata/connector.json')` — read the `install_fields` array (each field has label, key, help text)
3. For EACH install field, write a dedicated section explaining:
   - What this credential is and why it's needed
   - EXACTLY where to find it (portal URL, menu path, button name)
   - Screenshot hints (describe what the user will see)
   - Common gotchas / tips
4. Call `write_file('instructions/setup.md', content)` with the complete guide
5. Call `done('Instructions written successfully')`

## Output format for setup.md
```markdown
# {ConnectorName} Setup Guide

## Overview
One paragraph describing what this connector does and what access is required.

## Prerequisites
- Bullet list of any accounts, subscriptions, or access levels needed

## Step-by-Step Configuration

### 1. {Field Label} (`{field_key}`)
**What it is:** ...
**Where to find it:**
1. Go to [Portal Name](https://url)
2. Navigate to Settings → API Keys (or similar)
3. Click "Create New Key" / "Generate"
4. Copy the value shown

**Tip:** ...

### 2. {Next Field}
...

## Testing the Connection
After entering all credentials, click **Check Connection** to validate.

## Troubleshooting
Common errors and how to fix them.
```

## Rules
- Be SPECIFIC: name the exact menu, button, URL for THIS provider
- If you know the provider's portal URL, include it
- Use numbered steps within each section
- Keep it simple — assume the user is a non-developer admin
- Do NOT hallucinate — if unsure about a URL, describe the navigation path instead
"""


# ── generate_implementation_plan ─────────────────────────────────────

_IMPL_PLAN_SYSTEM = """You are a senior Python software architect designing an integration connector.

Your job is to produce `implementation_plan.md` — a detailed, SOC/OCP-compliant implementation
blueprint that the code generator will follow EXACTLY when writing connector.py.

## SOC (Separation of Concerns) rules
- Each class has ONE responsibility: client layer (HTTP), connector layer (business logic), config layer (settings)
- HTTP calls live ONLY in the client class — never directly in the Connector class methods
- Validation, credential checks, and error mapping live in the connector class
- Config loading lives in a dedicated config module

## OCP (Open/Closed Principle) rules
- Connector class is open for extension, closed for modification
- Error codes / status mappings use dicts/enums, not if/elif chains — adding a new code = adding a dict entry
- Client methods accept optional extra params (`**kwargs`) so callers can extend without changing signatures

## CRITICAL — Package naming
- The user message specifies the EXACT package root directory name — use it verbatim
- NEVER shorten or rename it (e.g. do NOT use `paytm/` when told to use `paytm_upi_connector/`)
- Every file path in the Package Structure section must be relative to that root directory

## SHARED VENV — Package Dependencies Rules (Section 7)
The connector runs inside a shared Python 3.13 virtual environment that has these packages
PRE-INSTALLED. Do NOT list any of them in Section 7:
  Pre-installed (omit from Section 7):
    pydantic, pydantic-settings, pydantic-core
    httpx
    structlog
    pytest, pytest-asyncio, pytest-mock
    google-auth, google-auth-oauthlib, google-auth-httplib2
    anyio, certifi, h11, sniffio, idna

Section 7 MUST contain ONLY connector-specific third-party packages (e.g. the provider's own SDK,
a specialised encoding library, a payment gateway client, etc.) that are NOT in the pre-installed list.

Version specifier rules for Section 7:
  ✅ CORRECT: `google-api-python-client>=2.100`   (minimum floor, not pinned)
  ✅ CORRECT: `tweepy>=4.14`
  ❌ WRONG:   `pydantic==2.9.0`                   (pre-installed — omit entirely)
  ❌ WRONG:   `httpx==0.27.0`                      (pre-installed — omit entirely)
  ❌ WRONG:   `google-api-python-client==2.111.0`  (exact pin causes rebuild → wheel failure)
  ❌ WRONG:   `-e /path/to/shielva-connectors`     (Shielva SDK is pre-installed — never editable install)

If the connector only needs packages from the pre-installed list (common for simple REST connectors),
write Section 7 with a single note: "No additional packages required — all dependencies are
pre-installed in the shared venv."

## Required document sections (ALL must be present):
1. Package Structure — directory tree rooted at the EXACT package root name given, each file's responsibility
2. Connector Class — full class name, AUTH_TYPE, all public method signatures with exact types
3. Client / SDK Layer — class name, constructor, every async method with signature and return type
4. Config Layer — config class, all fields, defaults, validation rules
5. Per-Method Implementation Guidelines — for EVERY connector method:
   - What it does (1 sentence)
   - Which client method(s) it calls
   - Error cases to handle and what to return
   - SOC boundary: what stays in client vs connector
6. Error Handling Strategy — exception hierarchy, HTTP status → ConnectorStatus mapping table
7. Package Dependencies — connector-specific packages ONLY (see Shared Venv rules above)
8. Package Structure Compliance — list any files beyond connector.py that must exist (helpers/, client/, etc.)
9. Test Compatibility Notes — what the test generator must know (mock targets, async patterns)

Write the FULL document. Minimum 5000 chars. No placeholders, no "TBD".
"""


async def handle_generate_implementation_plan(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Generate implementation_plan.md from API spec (via RAG) + scaffold context.

    Produces a SOC/OCP-compliant blueprint that write_connector uses as its specification.
    Stored locally at out_dir/implementation_plan.md and in R2.
    """
    from integration.services import r2_service as _r2
    from integration.services import knowledge_service as _ks

    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    provider   = context.get("provider", "")
    service_slug = context.get("service_slug", "")
    service_name = context.get("service_name", service_slug)
    auth_type  = context.get("auth_type", "unknown")
    user_prompt = context.get("user_prompt", "")

    await _emit(log_cb, "info", f"📐 Generating implementation plan for {service_name}...")

    # ── Read scaffold files for context ──────────────────────────────────────
    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            return ""

    scaffold_ctx = ""
    for fname in ["config.py", "requirements.txt", "__init__.py"]:
        content = _read(out_dir / fname)
        if content:
            scaffold_ctx += f"\n### {fname}\n```python\n{content[:1500]}\n```"

    # Also list any sub-directories already scaffolded
    existing_files = [str(f.relative_to(out_dir)) for f in sorted(out_dir.rglob("*.py"))
                      if "__pycache__" not in str(f)]
    scaffold_ctx += f"\n\n### Existing package files\n" + "\n".join(f"- {f}" for f in existing_files)

    # ── Query RAG for API spec knowledge (parallel) ───────────────────────────
    rag_context = ""
    try:
        queries = [
            f"{provider} {service_name} API methods endpoints",
            f"{provider} {service_name} authentication {auth_type}",
            f"{provider} {service_name} error codes response format",
            f"{provider} {service_name} refund status webhook",
        ]
        import asyncio as _asyncio_rag
        rag_results = await _asyncio_rag.gather(
            *[_ks.query_knowledge(
                query=q, tenant_id=context.get("tenant_id", ""),
                provider=provider, service=service_slug, top_k=4,
            ) for q in queries],
            return_exceptions=True,
        )
        rag_parts = [r for r in rag_results if r and not isinstance(r, Exception)]
        if rag_parts:
            rag_context = "\n\n---\n\n".join(rag_parts)
            await _emit(log_cb, "info", f"📚 RAG context retrieved ({len(rag_context)} chars)")
    except Exception as _e:
        await _emit(log_cb, "info", f"ℹ RAG query skipped: {_e}")

    import re as _re_impl
    _clean_slug = _re_impl.sub(r'_connector$', '', service_slug) if service_slug.endswith('_connector') else service_slug
    package_root = f"{_clean_slug}_connector"
    connector_name_ctx = context.get("connector_name", "") or service_name

    user_message = f"""Generate `implementation_plan.md` for the **{service_name}** connector.

## EXACT Package Root Directory
**`{package_root}/`** — this is the ONLY valid name for the top-level directory.
❌ Do NOT use `{service_slug}/`, `{provider}/`, or any other shortened form.
✅ Every file path in the Package Structure section MUST start under `{package_root}/`.

## Connector Name
{connector_name_ctx}

## User Requirements
{user_prompt}

## Auth Type
{auth_type}

## Scaffold Context (files already created)
{scaffold_ctx}

## API Spec Knowledge (from ingested docs)
{rag_context[:6000] if rag_context else "(No API docs ingested — infer from user requirements and provider knowledge)"}

## Task
1. Analyse all the above context
2. Produce a complete implementation_plan.md following ALL 9 required sections in the system prompt
3. Package Structure section MUST use `{package_root}/` as the root — not shortened
4. Every connector method MUST be listed with exact Python signature and implementation guidelines
5. Call write_file('implementation_plan.md', <FULL CONTENT>)
6. Call done('Implementation plan written: X chars')

❌ DO NOT write a stub or summary — write the complete document (minimum 1200 chars)
❌ DO NOT call done() before write_file('implementation_plan.md', ...)
"""

    # Enhance overlay — when the parent connector is seeded, extend its existing
    # implementation plan / surface; do NOT re-derive design choices from scratch.
    if context.get("is_enhance"):
        from integration.services.agentic_fix import _enhance_directive
        user_message += _enhance_directive(out_dir, artifact="plan",
                                           enhancement_ask=context.get("user_prompt", ""))

    plan_content: Optional[str] = None
    _min_chars = 5000

    # ── Gemini agentic path ───────────────────────────────────────────────────
    if False:  # gemini path disabled — Claude is the only backend codegen runtime
        try:
            from integration.services.agentic_fix import _gemini_agentic_loop, _PLAN_TOOLS, _r2_service as _agentic_r2
            result = await _gemini_agentic_loop(
                out_dir,
                system_prompt=_IMPL_PLAN_SYSTEM,
                initial_message=user_message,
                tools=_PLAN_TOOLS,   # read_file + write_file + done + search_knowledge only — no connector.py checks
                log_cb=log_cb,
                max_iterations=3,  # single-shot: think → write_file → done
                stop_on_done=True,
                require_file="implementation_plan.md",
                require_file_min_size=_min_chars,
            )
            impl_path = out_dir / "implementation_plan.md"
            if impl_path.exists():
                raw = impl_path.read_text(encoding="utf-8")
                if len(raw.strip()) >= _min_chars:
                    plan_content = raw
        except Exception as _e:
            await _emit(log_cb, "warn", f"Gemini impl plan gen failed ({_e}) — falling back to Claude")

    # ── Claude fallback (direct call, write file manually) ───────────────────
    if not plan_content:
        try:
            raw_prompt = await _get_prompt("IMPL_PLAN_SYSTEM", _IMPL_PLAN_SYSTEM)
            code = await call_llm_fix(
                [{"role": "user", "content": user_message}],
                system=raw_prompt,
                max_tokens=60000,
                on_chunk=_make_gemini_progress_cb(log_cb, "Implementation Plan"),
            )
            if code and len(code.strip()) >= _min_chars:
                plan_content = code.strip()
                impl_path = out_dir / "implementation_plan.md"
                impl_path.write_text(plan_content, encoding="utf-8")
        except Exception as _e:
            await _emit(log_cb, "error", f"Claude impl plan gen failed: {_e}")
            return {"status": "fail", "output": str(_e)}

    if not plan_content:
        return {"status": "fail", "output": "Implementation plan generation produced no content"}

    # ── Save locally ──────────────────────────────────────────────────────────
    impl_local = out_dir / "implementation_plan.md"
    impl_local.write_text(plan_content, encoding="utf-8")
    await _emit(log_cb, "info", f"Saved implementation_plan.md ({len(plan_content)} chars)")

    # ── Ingest into RAG + store in R2 in parallel ────────────────────────────
    import asyncio as _asyncio_save

    async def _ingest_rag():
        try:
            await _ks.ingest_step_output(
                content=plan_content,
                filename="implementation_plan.md",
                tenant_id=context.get("tenant_id", ""),
                provider=provider,
                service=service_slug,
                step_type="implementation_plan",
            )
            await _emit(log_cb, "info", "📥 Implementation plan ingested into RAG")
        except Exception as _e:
            await _emit(log_cb, "info", f"ℹ RAG ingest skipped: {_e}")

    async def _store_r2():
        try:
            await _r2.store_implementation_plan(provider, service_slug, plan_content)
            await _emit(log_cb, "info", f"Stored in R2: {provider}/{service_slug}/implementation_plan.md")
        except Exception as _e:
            await _emit(log_cb, "warn", f"R2 store skipped: {_e}")

    await _asyncio_save.gather(_ingest_rag(), _store_r2())

    await _emit(log_cb, "success",
        f"✅ Implementation plan ready ({len(plan_content)} chars) — write_connector will follow this spec")
    return {
        "status": "pass",
        "output": {
            "path": str(impl_local),
            "chars": len(plan_content),
        },
    }


# ── Load implementation plan helper (used by write_connector) ─────────────

async def _load_implementation_plan(out_dir: Path, provider: str, service_slug: str) -> str:
    """Load implementation_plan.md for this connector.

    Priority: local disk → R2. Returns empty string if not found or too short.
    """
    from integration.services import r2_service as _r2
    _MIN = 500

    local = out_dir / "implementation_plan.md"
    if local.exists():
        raw = local.read_text(encoding="utf-8")
        if len(raw.strip()) >= _MIN:
            return raw

    try:
        raw = await _r2.get_implementation_plan(provider, service_slug)
        if raw and len(raw.strip()) >= _MIN:
            return raw
    except Exception:
        pass
    return ""


_TEST_GUIDELINES_SYSTEM = """
## CRITICAL ANTI-HALLUCINATION RULES — READ BEFORE WRITING ANYTHING

The prompt will provide a "GROUND TRUTH" block with exact class names, exception names,
and config keys extracted directly from the connector source. You MUST:

1. USE THE EXACT CONNECTOR CLASS NAME from Ground Truth — never rename it, never alias it
   ❌ BAD: PaytmConnector, GmailConnector, ConnectorClass, YourConnector
   ✅ GOOD: Copy the exact name from "Connector class name" in Ground Truth

2. USE ONLY EXCEPTION CLASSES LISTED IN GROUND TRUTH — never invent new ones
   ❌ BAD: PaytmTransactionPendingError, PaytmTransactionFailedError (don't exist)
   ✅ GOOD: Only list exceptions from "Real exception classes" in Ground Truth

3. USE EXACT CONFIG KEYS FROM GROUND TRUTH — never use domain knowledge for key names
   ❌ BAD: "merchant_id", "checksum_secret" (Paytm domain names you invented)
   ✅ GOOD: Copy keys exactly from "Config fixture keys" in Ground Truth

4. ALWAYS USE BARE `from connector import` — NEVER use the full package path
   ❌ BAD: from paytm_upi_connector.connector import PaytmConnector
   ✅ GOOD: from connector import PaytmConnector
   The test runner sets cwd=<package>/ so bare `connector` resolves to connector.py directly.
   Using the full package path creates a SECOND module instance and breaks isinstance checks.

5. NEVER TRUNCATE — write the COMPLETE document. If the output would be long, that is fine.
   Every section must be complete. Section 9 must have a block for EVERY public method.

---

You are an expert Python test engineer analyzing a generated connector package.
Your job is to create a CONNECTOR-SPECIFIC test guideline document that will be
used by another AI (Gemini) to write pytest test cases for this exact connector.

## What you MUST output

A markdown document (test_guidelines.md) with these sections:

### 1. Package Structure
List every file in the connector package and what it does.

### 2. Connector Class
- Full class name (e.g. `YourConnector`)
- Import path: `from connector import YourConnector`
- AUTH_TYPE value
- All public methods with EXACT signatures (copy from code)

### 3. Client / SDK Layer
If there's a custom client (e.g. `client/http_client.py`):
- Class name and constructor signature EXACTLY as written
- Every async method with signature and return type
- How to import it: `from client.http_client import YourHttpClient`

### 4. Required Packages
List every import in connector.py and client files.
For each external package (not stdlib, not shared.*):
- Package name as it appears in requirements.txt
- Whether it is ALREADY in requirements.txt (check the file)
- If MISSING: add it to requirements.txt and note it was added

### 5. Fixture Blueprint (CRITICAL)
The EXACT pytest fixture code needed. Copy this pattern precisely:

```python
@pytest.fixture
def connector_config():
    return {
        # exact keys from install_fields in metadata/connector.json
        "key1": "test_value1",
        "key2": "test_value2",
    }

# If connector has a custom client class:
@pytest.fixture
def mock_ClientClass(mocker):
    with patch('connector.ClientClassName') as mock_cls:
        mock_instance = AsyncMock()
        # Set up each async method return value:
        mock_instance.method_name.return_value = {"key": "value"}
        mock_cls.return_value = mock_instance
        yield mock_cls, mock_instance

@pytest.fixture
def connector(connector_config, mock_ClientClass):
    mock_cls, mock_instance = mock_ClientClass
    return ConnectorClass(
        tenant_id="test-tenant-id",
        connector_id="test-connector-id",
        config=connector_config
    )
```

If there is NO custom client (uses httpx directly — httpx imported at module level):
```python
@pytest.fixture
def connector(connector_config):
    # httpx imported at module level → patch at "httpx.AsyncClient", NOT "connector.httpx.AsyncClient"
    with patch('httpx.AsyncClient') as mock_client:
        instance = ConnectorClass(tenant_id="test-tenant-id", connector_id="test-id", config=connector_config)
        instance._client = mock_client.return_value
        yield instance
```

### conftest.py autouse mock_storage fixture (REQUIRED for ALL connector types)
Every connector must have this autouse fixture that patches all BaseConnector storage methods.
These methods are defined on BaseConnector, so patch.object works WITHOUT `create=True`.
```python
@pytest.fixture(autouse=True)
def mock_storage(connector, mocker):
    mocker.patch.object(connector, "get_token", new=AsyncMock(return_value=None))
    mocker.patch.object(connector, "set_token", new=AsyncMock())
    mocker.patch.object(connector, "clear_token", new=AsyncMock())
    mocker.patch.object(connector, "save_config", new=AsyncMock())
    mocker.patch.object(connector, "get_metadata", new=AsyncMock(return_value=None))
    mocker.patch.object(connector, "set_metadata", new=AsyncMock())
    mocker.patch.object(connector, "ingest_batch", new=AsyncMock())
    mocker.patch.object(connector, "ingest_document", new=AsyncMock())
```

### 6. Mock Patterns Per Method
For each public method, show exactly how to mock the external call:
- Which method to mock (exact dotted path)
- Realistic return value matching the actual API response shape
- What to assert on the ConnectorStatus/SyncResult returned

### 7. Auth-type Specific Rules
Based on AUTH_TYPE:
- api_key/bearer/basic_auth: install() only validates keys present, no network call
- oauth2_code/oauth2_pkce: authorize() exchanges code for token
- service_account: base class handles auth, connector never calls authorize()

### 8. Do NOT Test
List what should NOT be tested (base class methods, infrastructure):
- get_token, set_token, clear_token, save_config, ingest_batch, ingest_document, get_metadata, set_metadata
- Any method from BaseConnector that is not overridden

### 9. Per-Method Test Specifications (REQUIRED — one entry per public method)

For EVERY public method in the connector class that should be tested, write a complete
test specification block with this EXACT structure:

```
#### Method: `method_name(param1, param2, ...)`

**Test scenarios to implement:**
1. [scenario name] — [one-line description of what to set up]
2. [scenario name] — [one-line description of what to set up]
3. [scenario name] — [one-line description]

**Mock setup for success case:**
```python
mock_instance.api_method.return_value = {
    # EXACT dict shape the real API returns for this operation
    "field1": "value1",
    "field2": 123,
}
```

**Required assertions (copy these into each test):**
```python
# Success case
assert result.status == ConnectorStatus.SUCCESS      # or PARTIAL / FAILED
assert result.health == ConnectorHealth.HEALTHY      # exact enum value
assert result.auth_status == AuthStatus.CONNECTED    # exact enum value
assert result.records_fetched == <expected_count>    # if applicable
assert result.records_synced == <expected_count>     # if applicable
assert "<key_field>" in result.metadata              # if method returns metadata

# Failure / error case (401/403)
assert result.health == ConnectorHealth.UNHEALTHY
assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

# Network error case
assert result.status == ConnectorStatus.FAILED
assert result.health == ConnectorHealth.OFFLINE
```

**Edge cases that MUST have their own test function:**
- What happens when the API returns an empty list / zero results
- What happens on HTTP 401 (invalid credentials)
- What happens on HTTP 429 or 500 (transient error)
- What happens when the client raises an exception (e.g. httpx.ConnectError)
```

MANDATORY methods to specify (always include these if they exist in the connector):

#### `install(config: dict) -> ConnectorStatus`
- Scenarios: all required keys present → CONNECTED; one key missing → MISSING_CREDENTIALS; empty config → MISSING_CREDENTIALS
- Assertions: install() must NEVER make a network call; check auth_status == CONNECTED when keys present

#### `health_check() -> ConnectorStatus`
- Scenarios: API returns 200 → HEALTHY+CONNECTED; API returns 401 → UNHEALTHY+INVALID_CREDENTIALS; API returns 500 → OFFLINE+UNKNOWN; client raises exception → OFFLINE
- Assertions: assert result.health, result.auth_status, result.status exactly

#### Every `sync_*` method (e.g. `sync_orders`, `sync_transactions`, `sync_invoices`)
- Scenarios: API returns N records → SUCCESS+records_fetched==N; API returns empty → SUCCESS+records_fetched==0; API returns 401; API raises exception
- Assertions: records_fetched, records_synced, health, auth_status, status

#### Every `get_*` / `fetch_*` / `search_*` method
- Scenarios: found → returns data; not found → empty/None; 401; exception
- Assertions: exact return type shape, health, auth_status

## CRITICAL FIELD/TYPE RULES — violations cause silent test failures

### ConnectorStatus fields
ConnectorStatus has ONLY these fields: connector_id, health, auth_status, message, metadata.
❌ NO `redirect_url` field — it does NOT exist on ConnectorStatus.
✅ OAuth redirect URLs go in metadata: `ConnectorStatus(..., metadata={"redirect_url": auth_url})`
✅ Test assertion: `assert result.metadata.get("redirect_url") == expected_url`

### sync() always returns SyncResult — never an async generator
`sync()` is a plain `async def` that returns a SyncResult.
✅ CORRECT: `result = await connector.sync(full=True)` → SyncResult with .status, .documents_synced
❌ WRONG: `async for doc in connector.sync(...): ...` — sync() is NOT an async generator

### NormalizedDocument requires source_id
Every NormalizedDocument must have both `id` and `source_id`:
✅ CORRECT: `NormalizedDocument(id="msg1", source_id="msg1", title="Test", content="Test", ...)`
❌ WRONG: `NormalizedDocument(id="msg1", title="Test", content="Test")` — missing source_id

## Rules
- Read ALL files in the package before writing the guideline
- Check requirements.txt and add any missing packages
- The fixture blueprint must be copy-pasteable and correct
- Section 9 MUST have one block per testable method — never skip this section
- Never guess — only document what is ACTUALLY in the code
- For assertions, use EXACT enum values (ConnectorHealth.HEALTHY, not "healthy")
- For mock return values, use the ACTUAL API response shape from the connector code
"""


async def _validate_and_patch_guidelines(
    guidelines: str,
    out_dir: Path,
    log_cb: LogCallback = None,
) -> str:
    """Validate generated test_guidelines.md against the actual connector source.

    Gemini commonly hallucinates:
      - The connector class name  (e.g. PaytmConnector instead of PaymentsConnector)
      - Exception class names     (e.g. PaytmTransactionPendingError which doesn't exist)
      - Config fixture keys       (e.g. merchant_id/checksum_secret vs client_id/client_secret)
      - Import paths              (e.g. 'from connector import X' instead of package path)

    This function reads the REAL source files, builds a ground-truth map, and does
    deterministic string-replacement corrections. No LLM needed — purely mechanical.
    """
    import re as _re
    import ast as _ast
    import json as _json

    corrections: List[str] = []

    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            return ""

    connector_src = _read(out_dir / "connector.py")
    exceptions_src = _read(out_dir / "exceptions.py")
    metadata_src = _read(out_dir / "metadata" / "connector.json")
    # Also check __init__.py for the exported name
    init_src = _read(out_dir / "__init__.py")

    patched = guidelines

    # ── 1. Find the REAL connector class name ─────────────────────────────────
    # Look for: class <Name>(BaseConnector): in connector.py
    real_class_name: str = ""
    for line in connector_src.splitlines():
        m = _re.match(r"^class\s+(\w+)\s*\(.*BaseConnector.*\)", line.strip())
        if m:
            real_class_name = m.group(1)
            break

    if real_class_name:
        # Find what name the guidelines claim (Section 2, "Full class name:")
        guideline_class_m = _re.search(r"Full class name:\s*[`*]*(\w+)[`*]*", patched)
        guideline_class = guideline_class_m.group(1) if guideline_class_m else ""

        if guideline_class and guideline_class != real_class_name:
            # Replace ALL occurrences of the wrong class name in the guidelines
            patched = patched.replace(guideline_class, real_class_name)
            corrections.append(f"class name: {guideline_class!r} → {real_class_name!r}")

        # Also catch cases where guidelines use the wrong name without it being in "Full class name:"
        # by scanning for any class names that look like connector classes but aren't the real one
        # Heuristic: any word ending in 'Connector' that isn't the real name
        fake_connector_names = set(_re.findall(r"\b(\w+Connector)\b", patched))
        fake_connector_names.discard(real_class_name)
        fake_connector_names.discard("BaseConnector")
        # Only replace names that look like they're meant to BE the main connector
        # (i.e. appear in fixture/patch.object/import contexts)
        for fake in fake_connector_names:
            # Check fake name doesn't actually exist in the real connector source
            if fake not in connector_src and fake not in exceptions_src and fake not in init_src:
                patched = patched.replace(fake, real_class_name)
                corrections.append(f"phantom connector class: {fake!r} → {real_class_name!r}")

    # ── 2. Find REAL exception class names ────────────────────────────────────
    # Extract all exception classes from connector.py + exceptions.py.
    # Only match classes that explicitly inherit from Exception/BaseException/Error lineage.
    real_exception_names: Set[str] = set()
    for src in (connector_src, exceptions_src):
        for line in src.splitlines():
            # Match: class FooError(SomeBaseException...) — must end in Error or Exception
            m = _re.match(r"^class\s+(\w+(?:Error|Exception))\s*\(", line.strip())
            if m:
                real_exception_names.add(m.group(1))

    # Known third-party exception names that are real but won't appear in connector source
    _KNOWN_EXTERNAL_EXCEPTIONS = {
        "ConnectError", "TimeoutException", "RequestError", "HTTPStatusError",
        "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    }

    if real_exception_names:
        # Find all exception-looking names mentioned in the guidelines
        guideline_exc_names = set(_re.findall(r"\b(\w+(?:Error|Exception))\b", patched))
        for exc in guideline_exc_names:
            if exc in (
                "Exception", "RuntimeError", "ValueError", "KeyError",
                "TypeError", "ImportError", "AttributeError", "OSError",
                "NotImplementedError", "StopIteration", "GeneratorExit",
            ):
                continue  # standard library — leave alone
            if exc in _KNOWN_EXTERNAL_EXCEPTIONS:
                continue  # httpx / known external — leave alone
            if exc not in real_exception_names and exc not in connector_src and exc not in exceptions_src:
                # This exception was hallucinated — find the closest real one by prefix matching
                # e.g. PaytmTransactionPendingError → PaytmAPIError (best available match)
                best_match = None
                # Try to find a real exception sharing the longest common prefix
                for real_exc in sorted(real_exception_names):
                    common = 0
                    for a, b in zip(exc, real_exc):
                        if a == b:
                            common += 1
                        else:
                            break
                    if best_match is None or common > best_match[0]:
                        best_match = (common, real_exc)
                if best_match and best_match[0] >= 4:  # at least 4 chars in common
                    patched = patched.replace(exc, best_match[1])
                    corrections.append(f"exception: {exc!r} → {best_match[1]!r}")
                # If no good match, leave it — better than replacing with something wrong

    # ── 3. Fix config fixture keys ────────────────────────────────────────────
    # Read install_fields from metadata/connector.json → these are the REAL config keys
    real_install_keys: List[str] = []
    if metadata_src:
        try:
            meta = _json.loads(metadata_src)
            real_install_keys = [f["key"] for f in meta.get("install_fields", []) if "key" in f]
        except Exception:
            pass

    if real_install_keys:
        # Find fixture dict keys in the guidelines (lines like "key": "value" inside code blocks)
        # Extract all string keys used in fixture dicts in the guidelines
        fixture_keys_in_guidelines = set(_re.findall(r'"([a-z][a-z0-9_]+)":\s*"[^"]*"', patched))
        fixture_keys_in_guidelines |= set(_re.findall(r"'([a-z][a-z0-9_]+)':\s*'[^']*'", patched))
        # Identify keys that look like connector config (non-standard pytest/Python keys)
        _standard_keys = {"role", "type", "label", "key", "default", "status", "url",
                          "level", "message", "error", "id", "name", "path"}
        config_looking_keys = {
            k for k in fixture_keys_in_guidelines
            if k not in _standard_keys and "_" in k or len(k) > 8
        }
        for wrong_key in config_looking_keys:
            if wrong_key not in real_install_keys:
                # Find the closest real key by substring match
                match = next(
                    (rk for rk in real_install_keys
                     if wrong_key in rk or rk in wrong_key or
                     wrong_key.replace("_", "") == rk.replace("_", "")),
                    None,
                )
                if match:
                    # Only replace inside fixture/config dict contexts (in code blocks)
                    patched = _re.sub(
                        rf'(?<=["\']){_re.escape(wrong_key)}(?=["\'])',
                        match,
                        patched,
                    )
                    corrections.append(f"config key: {wrong_key!r} → {match!r}")

    # ── 4. Fix import paths ───────────────────────────────────────────────────
    # The test runner sets cwd=<package>/ and includes that dir in PYTHONPATH.
    # So bare `from connector import X` resolves correctly to connector.py.
    # Using `from <package>.connector import X` would create a SECOND module instance
    # in sys.modules and break all exception identity checks.
    # Therefore: convert any full-path imports BACK to bare 'connector' imports.
    package_name = out_dir.name  # e.g. "paytm_upi_connector"
    if package_name and real_class_name:
        # If guidelines mistakenly use the full package path, revert to bare 'connector'
        n_before = len(patched)
        patched = _re.sub(
            rf"\bfrom {_re.escape(package_name)}\.connector import\b",
            "from connector import",
            patched,
        )
        # Also fix: patch('<package>.connector.X') → patch('connector.X')
        patched = _re.sub(
            rf"patch\(['\"]({_re.escape(package_name)})\.connector\.",
            "patch('connector.",
            patched,
        )
        if len(patched) != n_before:
            corrections.append(f"import path: '{package_name}.connector' → 'connector' (bare)")

    # ── 5. Fix hallucinated method names on the connector class ──────────────
    # Extract real public method names from the connector class via AST.
    # Replace any methods in the guidelines that don't exist in the real connector.
    if real_class_name and connector_src:
        try:
            tree = _ast.parse(connector_src)
            real_method_names: set[str] = set()
            for node in _ast.walk(tree):
                if isinstance(node, _ast.ClassDef) and node.name == real_class_name:
                    for item in node.body:
                        if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                            if not item.name.startswith("__"):
                                real_method_names.add(item.name)
                    break

            if real_method_names:
                # Find snake_case identifiers that appear SPECIFICALLY as method calls on
                # a connector variable (connector.X, await connector.X, await inst.X).
                # This is the most reliable signal — only fix things that the tests would
                # actually call, and avoid false positives on result.auth_status, mock.X, etc.

                # Pattern: optional 'await ', then a word that looks like a connector instance
                # variable (connector, inst, c, conn, paytm_connector, etc.), then .method_name(
                # We anchor on '(' because method calls always have parentheses.
                call_pattern = _re.compile(
                    r"(?:await\s+)?(?:connector|inst\b|conn\b|c\b|[a-z_]+_connector)\."
                    r"([a-z][a-z0-9_]+)\s*\("
                )
                # Also catch section headings: #### method_name
                heading_pattern = _re.compile(r"^#### +([a-z][a-z0-9_]+)\b", _re.MULTILINE)

                candidates_in_guidelines: set[str] = set()
                for m in call_pattern.finditer(patched):
                    candidates_in_guidelines.add(m.group(1))
                for m in heading_pattern.finditer(patched):
                    name = m.group(1)
                    if "_" in name:  # only snake_case headings
                        candidates_in_guidelines.add(name)

                fake_methods: set[str] = set()
                for candidate in candidates_in_guidelines:
                    if candidate not in real_method_names:
                        fake_methods.add(candidate)

                for fake_method in fake_methods:
                    fake_words = set(fake_method.split("_"))
                    best: tuple[int, str] | None = None
                    for real_m in real_method_names:
                        real_words = set(real_m.split("_"))
                        shared = len(fake_words & real_words)
                        if best is None or shared > best[0]:
                            best = (shared, real_m)
                    # Threshold: at least 1 shared meaningful word
                    if best and best[0] >= 1:
                        # Replace all occurrences (calls, headings, backtick signatures)
                        patched = _re.sub(rf"\b{_re.escape(fake_method)}\b", best[1], patched)
                        corrections.append(f"method: {fake_method!r} → {best[1]!r}")
        except Exception:
            pass  # AST parse failure — skip method patching

    # ── Report ────────────────────────────────────────────────────────────────
    if corrections:
        await _emit(log_cb, "warn",
                    f"⚠ Guidelines patched ({len(corrections)} corrections): {'; '.join(corrections)}")
    else:
        await _emit(log_cb, "info", "✓ Guidelines validated — all class/exception/config names correct")

    return patched


def _extract_connector_ground_truth(out_dir: Path) -> str:
    """Extract exact class names, exceptions, and config keys from the real connector source.

    Returns a markdown block to inject at the TOP of the Gemini prompt as locked constraints
    that Gemini MUST follow exactly — prevents hallucination of class names, exceptions, keys.
    """
    import ast as _ast, json as _json, re as _re

    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            return ""

    connector_src = _read(out_dir / "connector.py")
    exceptions_src = _read(out_dir / "exceptions.py")
    metadata_src = _read(out_dir / "metadata" / "connector.json")
    package_name = out_dir.name  # e.g. paytm_upi_connector

    # 1. Extract connector class name via AST
    connector_class = ""
    try:
        tree = _ast.parse(connector_src)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                for base in node.bases:
                    base_name = (base.id if isinstance(base, _ast.Name) else
                                 base.attr if isinstance(base, _ast.Attribute) else "")
                    if "BaseConnector" in base_name:
                        connector_class = node.name
                        break
            if connector_class:
                break
    except Exception:
        pass

    # 2. Extract method signatures for the connector class
    method_sigs = []
    if connector_class:
        try:
            tree = _ast.parse(connector_src)
            for node in _ast.walk(tree):
                if isinstance(node, _ast.ClassDef) and node.name == connector_class:
                    for item in node.body:
                        if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                            if not item.name.startswith("_"):
                                args = [a.arg for a in item.args.args]
                                method_sigs.append(
                                    f"  - {'async ' if isinstance(item, _ast.AsyncFunctionDef) else ''}"
                                    f"def {item.name}({', '.join(args)})"
                                )
        except Exception:
            pass

    # 3. Extract exception class names (only *Error/*Exception subclasses)
    exception_names = []
    for src in (exceptions_src, connector_src):
        try:
            tree = _ast.parse(src)
            for node in _ast.walk(tree):
                if isinstance(node, _ast.ClassDef) and (
                    node.name.endswith("Error") or node.name.endswith("Exception")
                ):
                    exception_names.append(node.name)
        except Exception:
            pass
    exception_names = list(dict.fromkeys(exception_names))  # deduplicate, preserve order

    # 4. Extract install_field keys from metadata
    config_keys = []
    try:
        meta = _json.loads(metadata_src)
        config_keys = [f["key"] for f in meta.get("install_fields", []) if "key" in f]
    except Exception:
        pass

    # If nothing was extractable (connector.py missing/unparseable), return empty string
    if not connector_class and not exception_names and not config_keys:
        return ""

    # Build the ground-truth block
    lines = [
        "## ⚠️ GROUND TRUTH — COPY THESE EXACTLY — DO NOT INVENT ALTERNATIVES",
        "",
        "The following facts were extracted directly from the connector source code.",
        "You MUST use these EXACT names. Any deviation will cause tests to fail.",
        "",
    ]
    if connector_class:
        lines += [
            f"**Connector class name:** `{connector_class}`",
            f"**Import path:** `from connector import {connector_class}`  ← BARE import, NOT `from {package_name}.connector import`",
            f"**Patch path prefix:** `connector.`  ← bare, NOT `{package_name}.connector.`",
            "",
        ]
    if exception_names:
        lines += [
            "**Real exception classes (ONLY these exist — do not invent others):**",
        ] + [f"  - `{e}`" for e in exception_names] + [""]
    if config_keys:
        lines += [
            "**Config fixture keys (from install_fields — use these EXACTLY):**",
        ] + [f'  - `"{k}": "test_value"`' for k in config_keys] + [""]
    if method_sigs:
        lines += [
            f"**Public methods on `{connector_class}` (copy signatures exactly):**",
        ] + method_sigs + [""]

    # 5. Extract non-connector, non-exception classes (e.g. PaytmClient, HttpClient)
    client_classes = []
    try:
        tree = _ast.parse(connector_src)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and node.name != connector_class:
                # Skip exception classes and enum/config classes
                if node.name.endswith(("Error", "Exception", "Config", "Settings", "Status", "Enum")):
                    continue
                # Check it's not an enum (has Enum base)
                bases = [
                    (b.id if isinstance(b, _ast.Name) else b.attr if isinstance(b, _ast.Attribute) else "")
                    for b in node.bases
                ]
                if any("Enum" in b or "Settings" in b or "BaseModel" in b for b in bases):
                    continue
                methods = []
                for item in node.body:
                    if isinstance(item, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                        if not item.name.startswith("_"):
                            args = [a.arg for a in item.args.args]
                            methods.append(
                                f"    - {'async ' if isinstance(item, _ast.AsyncFunctionDef) else ''}"
                                f"def {item.name}({', '.join(args)})"
                            )
                if methods:
                    client_classes.append((node.name, methods))
    except Exception:
        pass

    if client_classes:
        lines.append("**Client/helper classes defined in connector.py (mock these, do NOT use httpx directly):**")
        for cls_name, cls_methods in client_classes:
            lines.append(f"  - `{cls_name}` — patch as `patch('connector.{cls_name}')`")
            lines += cls_methods
        lines.append("")

    # 6. Extract top-level standalone functions (helpers like generate_checksum, verify_checksum)
    helper_fns = []
    try:
        tree = _ast.parse(connector_src)
        for node in tree.body:  # top-level only
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    args = [a.arg for a in node.args.args]
                    helper_fns.append(
                        f"  - `{'async ' if isinstance(node, _ast.AsyncFunctionDef) else ''}"
                        f"{node.name}({', '.join(args)})` — patch as `patch('connector.{node.name}')`"
                    )
    except Exception:
        pass

    if helper_fns:
        lines.append("**Top-level helper functions in connector.py (import and mock these directly):**")
        lines += helper_fns
        lines.append("")

    # 7. Extract connector __init__ self.attr = self.config.get(key) assignments
    # These are config attributes on the connector INSTANCE — tests must use connector.<attr>
    # NOT connector.client.<attr> (self.client is a Mock and has no config attributes).
    import re as _re2
    instance_attrs = []
    if connector_src and connector_class:
        # Match patterns like: self.merchant_key = self.config.get("merchant_key", ...)
        attr_pattern = _re2.compile(
            r'self\.(\w+)\s*=\s*self\.config\.(?:get|pop)\s*\(\s*["\'](\w+)["\']',
        )
        for match in attr_pattern.finditer(connector_src):
            attr_name, config_key = match.group(1), match.group(2)
            # Skip trivial attributes (not useful for assertions)
            if attr_name not in ("config", "connector_id", "tenant_id", "connector_type"):
                instance_attrs.append((attr_name, config_key))
        instance_attrs = list(dict.fromkeys(instance_attrs))  # deduplicate

    if instance_attrs:
        lines.append(
            f"**Connector instance attributes from config (use `connector.<attr>` in assertions, NEVER `connector.client.<attr>`):**"
        )
        for attr_name, config_key in instance_attrs:
            lines.append(
                f"  - `connector.{attr_name}` — set from `self.config.get(\"{config_key}\")` in __init__"
            )
        lines.append("")
        lines.append(
            "⚠️ `connector.client` is a MagicMock — it does NOT have these attributes. "
            "Always assert using `connector.<attr>` or the literal fixture config value."
        )
        lines.append("")

    lines += [
        "## END OF GROUND TRUTH",
        "",
    ]
    return "\n".join(lines)


async def handle_generate_test_guidelines(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Generate connector-specific test guidelines by reading the actual connector code.

    Reads connector.py, client files, config.py, requirements.txt, and metadata.
    Produces a detailed guideline markdown that write_tests will consume.
    Stored in R2 at {collection}/{provider}/{service_slug}/test_guidelines.md
    Also updates requirements.txt with any missing packages.
    """
    from integration.services import r2_service

    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    connector_path = out_dir / "connector.py"

    if not connector_path.exists():
        await _emit(log_cb, "error", "connector.py not found — run write_connector first")
        return {"status": "fail", "output": "connector.py missing"}

    await _emit(log_cb, "info", "Reading connector package structure...")

    # ── Read all relevant files ───────────────────────────────────────────────
    def _read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8") if p.exists() else ""
        except Exception:
            return ""

    connector_src = _read(connector_path)

    # Read all .py files in the package recursively
    package_files: Dict[str, str] = {}
    for f in sorted(out_dir.rglob("*.py")):
        rel = f.relative_to(out_dir)
        # Skip test files and __pycache__
        if "test" in str(rel) or "__pycache__" in str(rel):
            continue
        package_files[str(rel)] = _read(f)

    # Read requirements.txt
    req_path = out_dir / "requirements.txt"
    requirements_content = _read(req_path)

    # Read metadata
    metadata_path = out_dir / "metadata" / "connector.json"
    metadata_content = _read(metadata_path)

    # Read shared base_connector for reference (key sections only)
    base_connector_path = Path(__file__).parent.parent.parent / "shared" / "base_connector.py"
    base_src = _read(base_connector_path)
    # Only include the class signature and abstract methods — not full 800 lines
    import re as _re
    base_summary_lines = []
    for line in base_src.splitlines():
        stripped = line.strip()
        if any(kw in stripped for kw in [
            "class BaseConnector", "class ConnectorStatus", "class ConnectorHealth",
            "class AuthStatus", "class SyncResult", "class SyncStatus",
            "class TokenInfo", "class NormalizedDocument",
            "@abstractmethod", "async def ", "def __init__", "AUTH_TYPE",
        ]):
            base_summary_lines.append(line)
    base_summary = "\n".join(base_summary_lines[:150])  # cap at 150 lines

    # ── Build the prompt for Gemini / Claude ─────────────────────────────────
    provider = context.get("provider", "")
    service_slug = context.get("service_slug", "")
    service_name = context.get("service_name", service_slug)
    auth_type = context.get("auth_type", "unknown")

    files_section = ""
    for rel_path, content in package_files.items():
        # connector.py needs much more context — PaytmClient starts ~8k, PaytmConnector ~14k
        # Use a large limit for the main connector file; 3000 for everything else
        char_limit = 30000 if rel_path.endswith("connector.py") else 3000
        files_section += f"\n\n### {rel_path}\n```python\n{content[:char_limit]}\n```"
        if len(content) > char_limit:
            files_section += f"\n... (truncated, {len(content)} chars total)"

    _ground_truth_block = _extract_connector_ground_truth(out_dir)

    user_message = f"""Generate `test_guidelines.md` for the **{service_name}** connector.

{_ground_truth_block}
## Connector Package Files
{files_section}

## requirements.txt (current)
```
{requirements_content or "(empty)"}
```

## metadata/connector.json
```json
{metadata_content[:2000] if metadata_content else "(not found)"}
```

## BaseConnector API (key signatures)
```python
{base_summary}
```

## Task
1. Read ALL the files above carefully
2. Check which imported packages are NOT in requirements.txt — add them if missing
3. Call write_file('test_guidelines.md', <FULL CONTENT>) — write ALL 9 sections in FULL detail
   ❌ DO NOT write a summary sentence — write the complete markdown document (minimum 800 chars)
   ❌ DO NOT call done() before calling write_file('test_guidelines.md', ...)
4. Call done('Guidelines written: X chars') when finished

Auth type for this connector: **{auth_type}**
Provider: **{provider}**, Service slug: **{service_slug}**
"""

    # ── Use Gemini agentic or Claude fallback — hard 3-min timeout ───────────
    _GUIDELINES_TIMEOUT = 180  # seconds — fail fast rather than hang forever
    guidelines_content = None

    if False:  # gemini path disabled — Claude is the only backend codegen runtime
        try:
            from integration.services.agentic_fix import gemini_agentic_generate_test_guidelines
            result = await asyncio.wait_for(
                gemini_agentic_generate_test_guidelines(
                    out_dir,
                    context=context,
                    system_prompt=_TEST_GUIDELINES_SYSTEM,
                    user_message=user_message,
                    log_cb=log_cb,
                ),
                timeout=_GUIDELINES_TIMEOUT,
            )
            if result["success"]:
                guidelines_path = out_dir / "test_guidelines.md"
                if guidelines_path.exists():
                    guidelines_content = guidelines_path.read_text(encoding="utf-8")
                else:
                    guidelines_content = result.get("result", "")
        except asyncio.TimeoutError:
            await _emit(log_cb, "warn", f"Gemini guidelines gen timed out after {_GUIDELINES_TIMEOUT}s — falling back to Claude")
        except Exception as e:
            await _emit(log_cb, "warn", f"Gemini guidelines gen failed ({e}) — falling back to Claude")

    if not guidelines_content:
        # Claude fallback — with its own timeout
        try:
            from integration.services.llm_client import call_llm
            system_prompt = await _get_prompt("TEST_GUIDELINES_SYSTEM", _TEST_GUIDELINES_SYSTEM)
            guidelines_content = await asyncio.wait_for(
                call_llm(
                    messages=[{"role": "user", "content": user_message}],
                    system=system_prompt,
                    max_tokens=8000,
                    expect_code=False,
                ),
                timeout=_GUIDELINES_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await _emit(log_cb, "error", f"Claude guidelines gen timed out after {_GUIDELINES_TIMEOUT}s")
            return {"status": "fail", "output": f"Guidelines generation timed out after {_GUIDELINES_TIMEOUT}s — step failed"}
        except Exception as e:
            await _emit(log_cb, "error", f"Claude guidelines gen failed: {e}")
            return {"status": "fail", "output": str(e)}

    if not guidelines_content:
        return {"status": "fail", "output": "No guidelines generated"}

    # ── Validate & patch guidelines against actual connector source ───────────
    # Gemini often hallucinates class names, exception names, and config keys.
    # We read the real connector source and do deterministic string replacements
    # so the tests generated from these guidelines actually match the code.
    guidelines_content = await _validate_and_patch_guidelines(
        guidelines_content, out_dir, log_cb
    )

    # ── Save to local file ────────────────────────────────────────────────────
    guidelines_local_path = out_dir / "test_guidelines.md"
    guidelines_local_path.write_text(guidelines_content, encoding="utf-8")
    await _emit(log_cb, "info", f"Saved test_guidelines.md ({len(guidelines_content)} chars)")

    # ── Also update requirements.txt if packages were added ───────────────────
    # Check if the LLM updated it
    new_req = _read(req_path)
    if new_req != requirements_content and new_req:
        await _emit(log_cb, "info", "requirements.txt updated with missing packages")

    # ── Store in R2 ───────────────────────────────────────────────────────────
    try:
        await r2_service.store_test_guidelines(provider, service_slug, guidelines_content)
        await _emit(log_cb, "info", f"Stored in R2: {provider}/{service_slug}/test_guidelines.md")
    except Exception as e:
        await _emit(log_cb, "warn", f"R2 store skipped: {e}")

    await _emit(log_cb, "success", f"Test guidelines generated ({len(guidelines_content)} chars)")
    return {
        "status": "pass",
        "output": {
            "path": str(guidelines_local_path),
            "chars": len(guidelines_content),
            "r2_key": f"{provider}/{service_slug}/test_guidelines.md",
        }
    }


async def handle_setup_instructions(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Generate connector-specific setup instructions (instructions/setup.md).

    Uses Gemini to research the connector's provider and produce a step-by-step
    guide on where to find credentials for the deployment form.
    """
    provider = context.get("provider", "")
    service_slug = context["service_slug"]
    out_dir = _output_dir(context["tenant_id"], service_slug)
    connector_path = out_dir / "connector.py"
    metadata_path = out_dir / "metadata" / "connector.json"
    instructions_dir = out_dir / "instructions"
    instructions_path = instructions_dir / "setup.md"

    if not connector_path.exists():
        await _emit(log_cb, "error", "connector.py not found — run write_connector first")
        return {"status": "fail", "output": "connector.py missing"}

    await _emit(log_cb, "info", "Generating connector setup instructions…")

    # ── Gemini agentic path ────────────────────────────────────────────
    if False:  # gemini path disabled — Claude is the only backend codegen runtime
        from integration.services.agentic_fix import gemini_agentic_generate_instructions
        from integration.services.instructions_guidelines_service import get_instruction_guidelines
        guidelines = await get_instruction_guidelines()
        result = await gemini_agentic_generate_instructions(
            out_dir, context=context, guidelines=guidelines, log_cb=log_cb
        )
        if result["success"] and instructions_path.exists():
            content = instructions_path.read_text(encoding="utf-8")
            # RAG ingest the instructions
            try:
                await knowledge_service.ingest_step_output(
                    content=content,
                    filename="instructions/setup.md",
                    tenant_id=context["tenant_id"],
                    provider=context.get("provider", ""),
                    service=context.get("service", context.get("service_slug", "")),
                    step_type="setup_instructions",
                )
                await _emit(log_cb, "info", "Instructions ingested into knowledge base")
            except Exception as _e:
                await _emit(log_cb, "warn", f"RAG ingest skipped: {_e}")
            # Store to disk-first cache (then R2) as fallback for fetch
            try:
                await r2_service.store_setup_instructions(provider, service_slug, content)
                await _emit(log_cb, "info", f"Stored in R2: {provider}/{service_slug}/setup_instructions.md")
            except Exception as _e:
                await _emit(log_cb, "warn", f"R2 store skipped: {_e}")
            await _emit(log_cb, "success", f"✅ instructions/setup.md generated in {result['iterations']} iteration(s)")
            return {"status": "pass", "output": {"path": str(instructions_path), "chars": len(content)}}
        else:
            await _emit(log_cb, "warn", "Gemini agentic instructions gen incomplete — falling back to Claude")

    # ── Claude fallback ────────────────────────────────────────────────
    connector_source = connector_path.read_text(encoding="utf-8")
    metadata_content = ""
    if metadata_path.exists():
        metadata_content = metadata_path.read_text(encoding="utf-8")

    system_prompt = await _get_prompt("SETUP_INSTRUCTIONS_SYSTEM", _SETUP_INSTRUCTIONS_SYSTEM)
    # Inject provider/service context
    provider_hint = (
        f"\n\n## Connector Context\n"
        f"- Provider: {context.get('provider', 'unknown')}\n"
        f"- Service: {context.get('service_name', context.get('service', 'unknown'))}\n"
        f"- Auth Type: {context.get('auth_type', 'unknown')}\n"
    )

    user_msg = (
        f"Generate setup.md for this connector.\n{provider_hint}\n\n"
        f"## connector.py\n```python\n{connector_source[:6000]}\n```\n\n"
        f"## metadata/connector.json\n```json\n{metadata_content[:3000]}\n```\n\n"
        f"Output ONLY the markdown content for instructions/setup.md — no code fences around the entire doc."
    )

    try:
        raw = await call_llm_fix(
            [{"role": "user", "content": user_msg}],
            system=system_prompt,
            max_tokens=8000,
        )
        raw = raw.strip()
        if raw.startswith("```markdown"):
            raw = raw[len("```markdown"):].strip()
        if raw.startswith("```"):
            raw = raw[3:].strip()
        if raw.endswith("```"):
            raw = raw[:-3].strip()

        instructions_dir.mkdir(parents=True, exist_ok=True)
        instructions_path.write_text(raw, encoding="utf-8")

        # RAG ingest
        try:
            await knowledge_service.ingest_step_output(
                content=raw,
                filename="instructions/setup.md",
                tenant_id=context["tenant_id"],
                provider=context.get("provider", ""),
                service=context.get("service", context.get("service_slug", "")),
                step_type="setup_instructions",
            )
            await _emit(log_cb, "info", "Instructions ingested into knowledge base")
        except Exception as _e:
            await _emit(log_cb, "warn", f"RAG ingest skipped: {_e}")

        # Store to disk-first cache (then R2) as fallback for fetch
        try:
            await r2_service.store_setup_instructions(provider, service_slug, raw)
            await _emit(log_cb, "info", f"Stored in R2: {provider}/{service_slug}/setup_instructions.md")
        except Exception as _e:
            await _emit(log_cb, "warn", f"R2 store skipped: {_e}")

        await _emit(log_cb, "success", f"instructions/setup.md written ({len(raw)} chars)")
        return {"status": "pass", "output": {"path": str(instructions_path), "chars": len(raw)}}

    except Exception as e:
        await _emit(log_cb, "error", f"setup_instructions failed: {e}")
        return {"status": "fail", "output": str(e)}


async def handle_generate_metadata(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Generate metadata/connector.json from the built connector.py.

    Reads the generated connector source, calls the LLM to extract install fields
    and API catalogue, then writes metadata/connector.json.
    """
    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    connector_path = out_dir / "connector.py"
    metadata_dir = out_dir / "metadata"
    metadata_path = metadata_dir / "connector.json"

    if not connector_path.exists():
        await _emit(log_cb, "error", "connector.py not found — run write_connector first")
        return {"status": "fail", "output": "connector.py missing"}

    connector_source = connector_path.read_text(encoding="utf-8")
    _cfg_version = config.get("version", "1.0.0")
    version = "1.0.0" if not _cfg_version or _cfg_version == "auto" else _cfg_version

    # Bump version if metadata already exists
    if metadata_path.exists():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            parts = existing.get("version", "1.0.0").split(".")
            if len(parts) == 3:
                parts[2] = str(int(parts[2]) + 1)
                version = ".".join(parts)
        except Exception:
            pass

    await _emit(log_cb, "info", f"Generating metadata/connector.json (version {version})…")

    # ── Gemini agentic metadata generation ──────────────────────────────────
    if False:  # gemini path disabled — Claude is the only backend codegen runtime
        from integration.services.agentic_fix import gemini_agentic_generate_metadata
        result = await gemini_agentic_generate_metadata(
            out_dir,
            version=version,
            is_enhance=bool(context.get("is_enhance")),
            enhancement_ask=context.get("user_prompt", ""),
            log_cb=log_cb,
        )
        if result["success"] and metadata_path.exists():
            try:
                meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                await _emit(log_cb, "success", f"✅ connector.json generated (v{meta.get('version', version)}) in {result['iterations']} iteration(s)")
                return {"status": "pass", "output": meta}
            except json.JSONDecodeError:
                await _emit(log_cb, "warn", "Agentic metadata JSON invalid — falling back")
        else:
            await _emit(log_cb, "warn", "Agentic metadata gen incomplete — falling back to Claude")
        # Fall through to Claude below

    def _build_user_msg(source: str) -> str:
        return (
            f"Generate connector.json for this connector. Use version='{version}'.\n\n"
            f"```python\n{source}\n```"
        )

    def _extract_signatures(source: str) -> str:
        """Return only class-level lines + async def signatures to reduce token count on retry."""
        lines = source.splitlines()
        sig_lines: list[str] = []
        in_docstring = False
        for line in lines:
            stripped = line.strip()
            # Track docstrings to skip them
            if stripped.startswith('"""') or stripped.startswith("'''"):
                if in_docstring:
                    in_docstring = False
                    continue
                # Single-line docstring
                if stripped.count('"""') >= 2 or stripped.count("'''") >= 2:
                    continue
                in_docstring = True
                continue
            if in_docstring:
                continue
            # Keep class declarations, CONNECTOR_TYPE assignments, method signatures, config lines
            if (
                stripped.startswith("class ")
                or stripped.startswith("CONNECTOR_TYPE")
                or stripped.startswith("async def ")
                or stripped.startswith("def ")
                or "self.config" in stripped
                or stripped.startswith("VERSION")
                or stripped.startswith("version")
            ):
                sig_lines.append(line)
        return "\n".join(sig_lines)

    def _clean_raw(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw.strip())
        return raw

    # Load metadata system prompt from R2 (live-editable) with hardcoded fallback
    _meta_system_tpl = await _get_prompt("METADATA_SYSTEM_PROMPT", _METADATA_SYSTEM_PROMPT)

    # Build context-aware metadata prompt (Gemini now knows the connector's identity)
    _meta_system = _meta_system_tpl.format(
        provider=_get_ctx(context, "provider", "unknown"),
        service_name=_get_ctx(context, "service_name", "Unknown Service"),
        connector_name=_get_ctx(context, "connector_name", _get_ctx(context, "service_name")),
        auth_type=_get_ctx(context, "auth_type", "unknown"),
        sdk_package=_get_ctx(context, "sdk_package"),
        user_prompt=_get_ctx(context, "user_prompt", "(not provided)"),
        step_memory_summary=_build_step_memory_summary(context),
    )
    # Inject RAG knowledge — includes metadata_writing_guideline.md from global KB
    _meta_system = await _inject_rag_context(
        _meta_system,
        {**context, "step_type": "generate_metadata"},
    )

    try:
        # First attempt — full source, generous token budget
        raw = await call_llm_fix(
            [{"role": "user", "content": _build_user_msg(connector_source)}],
            system=_meta_system,
            max_tokens=60000,
        )
        raw = _clean_raw(raw)

        try:
            metadata = json.loads(raw)
        except json.JSONDecodeError as first_err:
            # JSON was truncated — retry with only method signatures to shrink prompt+response size.
            await _emit(log_cb, "warn",
                        f"First attempt produced invalid JSON ({first_err}) — retrying with condensed source…")
            condensed = _extract_signatures(connector_source)
            raw2 = await call_llm_fix(
                [{"role": "user", "content": _build_user_msg(condensed)}],
                system=_meta_system,
                max_tokens=60000,
            )
            raw2 = _clean_raw(raw2)
            metadata = json.loads(raw2)  # let this propagate if still broken

        metadata["version"] = version  # ensure version matches

        # Always override connector_type with the actual CONNECTOR_TYPE constant from
        # connector.py — the LLM sometimes derives a shorter/different value (e.g. "paytm"
        # instead of "paytm_payments") which causes "Connector type not found" at deploy time.
        _ct_match = re.search(r'CONNECTOR_TYPE\s*=\s*["\']([^"\']+)["\']', connector_source)
        if _ct_match:
            metadata["connector_type"] = _ct_match.group(1)
        else:
            # CONNECTOR_TYPE is missing from connector.py — this is a hard error because the
            # gateway won't be able to load the connector at deploy time. Fail loudly so the
            # LLM is forced to add it rather than silently producing a broken artifact.
            service_slug = context.get("service_slug", "unknown")
            import re as _re_ct
            _clean = _re_ct.sub(r'_connector$', '', service_slug) if service_slug.endswith('_connector') else service_slug
            await _emit(
                log_cb, "error",
                f"connector.py is missing the CONNECTOR_TYPE class attribute. "
                f"The gateway uses this to register the connector — without it "
                f"POST /connectors/deploy returns 404. "
                f"Add this line inside the connector class: CONNECTOR_TYPE = \"{_clean}\". "
                f"Fix connector.py and re-run this step."
            )
            return {"status": "fail", "output": "connector.py missing CONNECTOR_TYPE class attribute"}

        metadata_dir.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        install_field_count = len(metadata.get("install_fields", []))
        api_count = len(metadata.get("apis", []))
        await _emit(log_cb, "success",
                    f"metadata/connector.json written — {install_field_count} install fields, {api_count} APIs, version {version}")

        return {
            "status": "pass",
            "output": {
                "path": str(metadata_path),
                "version": version,
                "install_fields": install_field_count,
                "apis": api_count,
            },
        }

    except json.JSONDecodeError as e:
        await _emit(log_cb, "error", f"LLM returned invalid JSON: {e}")
        return {"status": "fail", "output": f"JSON parse error: {e}"}
    except Exception as e:
        await _emit(log_cb, "error", f"generate_metadata failed: {e}")
        return {"status": "fail", "output": str(e)}


# ── smoke_test ───────────────────────────────────────────────────────

async def handle_smoke_test(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Run the connector smoke test after all files have been generated.

    Imports connector.py in a subprocess with all network calls mocked and verifies:
    - The connector class can be imported without errors
    - The class can be instantiated
    - install() runs without raising exceptions and returns a valid ConnectorStatus
    """
    from integration.services.agentic_fix import run_connector_smoke_test

    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    connector_path = out_dir / "connector.py"

    if not connector_path.exists():
        await _emit(log_cb, "error", "connector.py not found — run write_connector first")
        return {"status": "fail", "output": "connector.py missing"}

    await _emit(log_cb, "info", "🔬 Running connector smoke test (import + install() with mocked network)...")

    result = await run_connector_smoke_test(out_dir)

    if result.startswith("SMOKE TEST PASSED"):
        await _emit(log_cb, "success", f"✅ {result}")
        return {"status": "pass", "output": result}
    else:
        await _emit(log_cb, "error", result)
        return {"status": "fail", "output": result}


def _try_recover_truncated_code(code: str, exc: SyntaxError) -> Optional[str]:
    """Try to recover LLM output truncated mid-triple-quoted string.

    When max_tokens is hit mid-docstring the LLM stops inside a triple-quoted
    literal. Attempt to close the open string and make the trailing block valid
    by appending `pass`.
    """
    if "unterminated" not in str(exc).lower():
        return None
    stripped = code.rstrip()
    # Try closing with both quote types + indented pass (common: inside a method body)
    for close_q in ('"""', "'''"):
        for tail in (f'\n    {close_q}\n    pass\n', f'\n{close_q}\n'):
            candidate = stripped + tail
            try:
                ast.parse(candidate)
                return candidate
            except SyntaxError:
                pass
    return None


def _fast_fix_smoke_imports(code: str) -> str:
    """Fix absolute sub-package imports → relative (common smoke test failure pattern).

    Generated connectors often use `from client import X` or `from client.X import Y`
    inside their package, which fails when imported as a package because Python looks
    for a top-level module named `client`.  Relative imports always work correctly.

    Covers:
      from client import X          → from .client import X
      from client.X import Y        → from .client.X import Y
      from exceptions import X      → from .exceptions import X
      from helpers import X         → from .helpers import X
      from helpers.X import Y       → from .helpers.X import Y
      from utils import X           → from .utils import X
      from models import X          → from .models import X
      from constants import X       → from .constants import X
      from config import X          → from .config import X  (only if not already relative)
      from .client.client import X  → from .client import X  (double-name collapsed)
      from .client.utils import X   → from .client import X  (if utils in client submodule)
    """
    import re

    # Known local sub-module names generated connectors commonly produce
    _LOCAL_MODS = r'(client|exceptions|helpers|utils|models|constants|auth|config|types|errors|api)'

    # "from client import X"  →  "from .client import X"
    # "from client.sub import X" → "from .client.sub import X"
    # Negative lookbehind ensures we don't double-add the dot
    code = re.sub(
        r'^(from )(?!\.)(' + _LOCAL_MODS + r')([\. ])',
        r'\1.\2\3',
        code,
        flags=re.MULTILINE,
    )

    # Fix double-name pattern: "from .X.X import Y" → "from .X import Y"
    # Happens when LLM generates "from .client.client import PaytmClient" but
    # the file is just client.py at package root (no client/ subdirectory).
    code = re.sub(
        r'^(from \.)(\w+)\.\2( import)',
        r'\1\2\3',
        code,
        flags=re.MULTILINE,
    )

    return code


async def handle_fix_smoke_test(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Fix connector.py when the smoke test fails.

    Phase 1  — deterministic fast-fix: converts absolute sub-package imports to
               relative imports (the most common smoke test failure pattern).
    Phase 1b — directory-aware import fix: collapses broken "from .X.Y import"
               when X/ subdirectory doesn't exist on disk (e.g. from .client.client
               → from .client when there's no client/ folder).
    Phase 2  — LLM fix: if deterministic fixes don't resolve the failure, uses
               handle_fix_connector then verifies with a fresh smoke test.
    """
    import re as _re
    import ast as _ast
    from integration.services.agentic_fix import run_connector_smoke_test

    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    connector_path = out_dir / "connector.py"

    if not connector_path.exists():
        await _emit(log_cb, "error", "connector.py not found — cannot fix smoke test")
        return {"status": "fail", "output": "connector.py missing"}

    code = connector_path.read_text(encoding="utf-8")

    # ── Phase 1: deterministic relative-import fast-fix ─────────────────────
    fixed_code = _fast_fix_smoke_imports(code)
    if fixed_code != code:
        connector_path.write_text(fixed_code, encoding="utf-8")
        await _emit(log_cb, "info", "Fast-fix Phase 1: absolute → relative sub-package imports")
        result = await run_connector_smoke_test(out_dir)
        if result.startswith("SMOKE TEST PASSED"):
            await _emit(log_cb, "success", result)
            return {"status": "pass", "output": result}
        await _emit(log_cb, "warn", "Phase 1 fix incomplete — running Phase 1b directory check")
        code = fixed_code  # pass phase-1 result into phase-1b

    # ── Phase 1b: directory-aware sub-package import fix ─────────────────────
    # Fixes patterns like "from .client.client import X" when client/ directory
    # doesn't exist on disk.  The generated code assumes a client/ subpackage but
    # only a flat client.py file was actually written.
    _sub_import_re = _re.compile(r'^(from \.)(\w+)\.(\w+)( import)', _re.MULTILINE)
    phase1b_changes: list = []
    phase1b_code = code

    for match in _sub_import_re.finditer(code):
        pkg_name = match.group(2)  # e.g. "client"
        mod_name = match.group(3)  # e.g. "client" / "utils"
        subdir = out_dir / pkg_name
        if not subdir.is_dir():
            # Sub-package directory does not exist → collapse to flat module import
            # "from .client.client import X" → "from .client import X"
            # "from .client.utils import X" → "from .client import X" (utils doesn't exist in subdir)
            old = match.group(0)
            new = f"{match.group(1)}{pkg_name}{match.group(4)}"
            phase1b_code = phase1b_code.replace(old, new, 1)
            phase1b_changes.append(f"  {old.strip()} → {new.strip()}")

    if phase1b_changes:
        # Only write if the result is syntactically valid
        try:
            _ast.parse(phase1b_code)
            connector_path.write_text(phase1b_code, encoding="utf-8")
            await _emit(log_cb, "info",
                f"Fast-fix Phase 1b: collapsed {len(phase1b_changes)} non-existent sub-package import(s):\n" +
                "\n".join(phase1b_changes))
            result = await run_connector_smoke_test(out_dir)
            if result.startswith("SMOKE TEST PASSED"):
                await _emit(log_cb, "success", result)
                return {"status": "pass", "output": result}
            await _emit(log_cb, "warn", "Phase 1b fix incomplete — handing to LLM fix")
            code = phase1b_code  # pass into LLM with already-partially-fixed code
        except SyntaxError as _se:
            await _emit(log_cb, "warn", f"Phase 1b would introduce syntax error — skipping: {_se}")

    # ── Phase 2: LLM fix ─────────────────────────────────────────────────────
    error_details = context.get("error_details", "Smoke test failed — see output above")
    fix_result = await handle_fix_connector(
        config,
        {**context, "error_details": error_details},
        log_cb,
    )
    if fix_result.get("status") != "pass":
        # LLM fix wrote bad code (syntax error) or failed entirely — try once more
        # by re-reading whatever is on disk (may still be phase-1b fixed version)
        await _emit(log_cb, "warn", "LLM fix failed — running smoke test with current connector.py")
        result = await run_connector_smoke_test(out_dir)
        if result.startswith("SMOKE TEST PASSED"):
            await _emit(log_cb, "success", result)
            return {"status": "pass", "output": result}
        await _emit(log_cb, "error", result)
        return {"status": "fail", "output": result}

    # Verify with a fresh smoke test run
    result = await run_connector_smoke_test(out_dir)
    if result.startswith("SMOKE TEST PASSED"):
        await _emit(log_cb, "success", result)
        return {"status": "pass", "output": result}
    else:
        await _emit(log_cb, "error", result)
        return {"status": "fail", "output": result}


# ── implement_persistence ─────────────────────────────────────────────

async def handle_implement_persistence(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """For each api_response_persistent method:
    1. Reads entity builder config from R2.
    2. Generates a tenant-specific repository class under repository/.
    3. Verifies connector.py has the persistence helper already (apply-persistence ran).
    4. Runs the method's test cases to confirm persistence works.
    """
    from integration.services.r2_service import get_entity_builder_config
    from integration.db.database import sessions_collection as _sc
    from bson import ObjectId as _OId

    tenant_id = context["tenant_id"]
    provider = _get_ctx(context, "provider", "unknown")
    service_slug = context.get("service_slug") or context.get("connector_name", "unknown")
    session_id = context.get("session_id")

    out_dir = _output_dir(tenant_id, service_slug)

    await _emit(log_cb, "info", "Checking for api_response_persistent methods...")

    # Load method identities from session
    persistent_methods: List[Dict] = []
    if session_id:
        try:
            doc = await _sc().find_one(
                {"_id": _OId(session_id), "tenant_id": tenant_id},
                {"method_identities": 1},
            )
            if doc:
                persistent_methods = [
                    mi for mi in (doc.get("method_identities") or [])
                    if mi.get("identity") == "api_response_persistent" and mi.get("entity_id")
                ]
        except Exception as _e:
            await _emit(log_cb, "warn", f"Could not load method identities from session: {_e}")

    if not persistent_methods:
        await _emit(log_cb, "info", "No api_response_persistent methods configured — step is a no-op")
        return {"status": "pass", "output": {"skipped": True, "reason": "no persistent methods"}}

    repo_dir = out_dir / "repository"
    repo_dir.mkdir(exist_ok=True)

    # Write repository/__init__.py
    init_py = repo_dir / "__init__.py"
    if not init_py.exists():
        init_py.write_text("", encoding="utf-8")

    results: List[Dict] = []

    for mi in persistent_methods:
        method_name = mi["method_name"]
        entity_id = mi.get("entity_id", "")

        await _emit(log_cb, "info", f"Processing method: {method_name}")

        # Load entity builder config from R2
        eb_config = await get_entity_builder_config(provider, service_slug, method_name)
        if not eb_config:
            await _emit(log_cb, "warn", f"No R2 entity builder config for {method_name} — using session data")
            eb_config = {
                "method_name": method_name,
                "entity_config": next(
                    (ec for ec in (doc or {}).get("entity_configs", []) if ec.get("entity_id") == entity_id),
                    {"collection_name": "results", "database_name": "connector_data"},
                ),
                "field_mappings": mi.get("field_mappings", []),
            }

        entity_cfg = eb_config.get("entity_config", {})
        collection_name = entity_cfg.get("collection_name", "results")
        database_name = entity_cfg.get("database_name", "connector_data")
        field_mappings = eb_config.get("field_mappings", [])

        import re as _re2
        _clean_slug = _re2.sub(r"_connector$", "", service_slug) if service_slug.endswith("_connector") else service_slug
        connector_class_name = "".join(w.capitalize() for w in _clean_slug.replace("-", "_").split("_"))
        repo_class_name = eb_config.get("repo_class") or f"{connector_class_name}Repository"
        repo_module_name = eb_config.get("repo_module") or f"{_clean_slug}_repository"
        repo_file = repo_dir / f"{repo_module_name}.py"

        if repo_file.exists():
            # Repository was already generated by apply-persistence — just verify
            await _emit(log_cb, "info", f"Repository file already exists: {repo_file.name} — skipping regeneration")
        else:
            # Generate repository file (fallback: apply-persistence may not have run)
            mapping_lines = []
            for fm in field_mappings:
                resp_path = fm.get("response_path", "")
                entity_field = fm.get("entity_field", "")
                transform = fm.get("transform", "")
                if transform:
                    mapping_lines.append(f'            "{entity_field}": {transform},')
                else:
                    mapping_lines.append(f'            "{entity_field}": response.get("{resp_path}"),')
            mappings_str = "\n".join(mapping_lines) if mapping_lines else '            **response,'

            repo_code = f'''"""Auto-generated repository for {_clean_slug} connector.

Tenant isolation: database = {{tenant_id}}_{database_name}
Generated by Shielva integration builder — do not edit manually.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from shared.repository_service import BaseRepository
from typing import Any, Dict


class {repo_class_name}(BaseRepository):
    DATABASE_NAME = "{database_name}"

    async def save_{method_name}_result(self, response: Dict[str, Any]) -> str:
        """Persist {method_name} API response to collection: {collection_name}."""
        document = {{
{mappings_str}
        }}
        return await self.insert_one("{collection_name}", document)
'''
            repo_file.write_text(repo_code, encoding="utf-8")
            await _emit(log_cb, "success", f"Generated {repo_file.name} ({repo_class_name})")

        # Verify connector.py uses the repository (has helper + import)
        connector_py = out_dir / "connector.py"
        if connector_py.exists():
            source = connector_py.read_text(encoding="utf-8")
            helper_name = f"_persist_{method_name}_result"
            has_helper = helper_name in source
            has_import = repo_class_name in source
            if has_helper and has_import:
                await _emit(log_cb, "success",
                    f"connector.py uses {repo_class_name} with {helper_name}() — persistence wired correctly")
                results.append({"method": method_name, "status": "pass", "repository": str(repo_file)})
            elif has_helper:
                await _emit(log_cb, "warn",
                    f"connector.py has {helper_name}() but missing import of {repo_class_name}")
                results.append({"method": method_name, "status": "warn", "repository": str(repo_file)})
            else:
                await _emit(log_cb, "warn",
                    f"connector.py missing {helper_name}() — apply-persistence may not have run yet")
                results.append({"method": method_name, "status": "warn", "repository": str(repo_file)})
        else:
            await _emit(log_cb, "error", "connector.py not found")
            results.append({"method": method_name, "status": "fail"})

    all_ok = all(r["status"] in ("pass", "warn") for r in results)
    return {
        "status": "pass" if all_ok else "fail",
        "output": {
            "methods": results,
            "repository_dir": str(repo_dir),
        },
    }


# ── write_integration_tests ──────────────────────────────────────────

async def handle_write_integration_tests(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Use LLM to generate tests/test_integration.py — real API calls with user credentials."""
    from integration.prompts.codegen_prompt import INTEGRATION_TEST_SYSTEM_PROMPT
    import json as _json

    out_dir = _output_dir(context["tenant_id"], context["service_slug"])
    connector_path = out_dir / "connector.py"

    if not connector_path.exists():
        await _emit(log_cb, "error", "connector.py not found — run write_connector first")
        return {"status": "fail", "output": "connector.py missing"}

    connector_code = connector_path.read_text(encoding="utf-8")

    # Extract class name from connector.py
    _class_match = re.search(r"^class\s+(\w+)\s*\(BaseConnector\)", connector_code, re.MULTILINE)
    class_name = _class_match.group(1) if _class_match else context.get("class_name", "Connector")

    # Load install_fields from connector.json if present
    install_fields: list = []
    connector_json_path = out_dir / "connector.json"
    if not connector_json_path.exists():
        connector_json_path = out_dir / "metadata" / "connector.json"
    if connector_json_path.exists():
        try:
            _meta = _json.loads(connector_json_path.read_text(encoding="utf-8"))
            install_fields = _meta.get("install_fields", [])
        except Exception:
            pass

    # Fallback: extract field keys from AST (self.config.get("key", ...))
    if not install_fields:
        _facts = _extract_connector_ground_truth(out_dir)
        # _extract_connector_ground_truth returns a string; parse field keys separately
        try:
            _tree = ast.parse(connector_code)
            for _node in ast.walk(_tree):
                if (isinstance(_node, ast.Call)
                        and isinstance(getattr(_node.func, "attr", None), str)
                        and _node.func.attr == "get"
                        and isinstance(getattr(_node.func, "value", None), ast.Attribute)
                        and _node.func.value.attr == "config"):
                    if _node.args and isinstance(_node.args[0], ast.Constant):
                        _k = str(_node.args[0].value)
                        if _k not in install_fields:
                            install_fields.append(_k)
        except Exception:
            pass

    # Build prompt template variables
    _field_keys = [f["key"] if isinstance(f, dict) else str(f) for f in install_fields]
    # Filter out generic/internal keys that aren't user-supplied credentials
    _skip_keys = {"redirect_uri", "scopes", "webhook_secret"}
    _cred_keys = [k for k in _field_keys if k not in _skip_keys]

    _env_example = " ".join(f'{k}="<your_{k}>"' for k in _cred_keys) if _cred_keys else 'api_key="<your_api_key>"'
    _config_dict = "\n        ".join(
        f'"{k}": os.environ.get("{k}", ""),' for k in _cred_keys
    ) if _cred_keys else '"api_key": os.environ.get("api_key", ""),'
    _fields_detail = "\n".join(
        f"  - `{k}`" for k in _cred_keys
    ) if _cred_keys else "  - (no install_fields found — check connector.json)"

    await _emit(log_cb, "info", f"Generating integration tests via {_llm_label()} (class={class_name}, fields={_cred_keys})...")

    system = (await _get_prompt("INTEGRATION_TEST_SYSTEM_PROMPT", INTEGRATION_TEST_SYSTEM_PROMPT)).format(
        connector_code=connector_code,
        provider=_get_ctx(context, "provider", "unknown"),
        service_name=_get_ctx(context, "service_name", "Unknown Service"),
        connector_name=_get_ctx(context, "connector_name", _get_ctx(context, "service_name")),
        auth_type=_get_ctx(context, "auth_type", "unknown"),
        user_prompt=_get_ctx(context, "user_prompt", "(not provided)"),
        step_memory_summary=_build_step_memory_summary(context),
        class_name=class_name,
        install_fields_env_example=_env_example,
        install_fields_config_dict=_config_dict,
        install_fields_detail=_fields_detail,
    )

    messages = [
        {"role": "user", "content": "Output integration test code for tests/test_integration.py. Return ONLY raw Python — no prose, no markdown, no tool calls."},
    ]

    try:
        code = await call_llm_fix(messages, system=system, max_tokens=16000)
        code = _clean_llm_code_response(code)

        if not code or len(code) < 50 or not code.lstrip().startswith(_VALID_PYTHON_STARTS):
            await _emit(log_cb, "error", f"LLM returned invalid response for integration tests: {code[:120]}")
            return {"status": "fail", "output": f"LLM did not return valid Python: {code[:200]}"}

        # Validate syntax
        try:
            ast.parse(code)
        except SyntaxError as exc:
            await _emit(log_cb, "warn", f"Integration test syntax error at line {exc.lineno} — {exc}")
            return {"status": "fail", "output": f"Generated integration test code has syntax error: {exc}"}

        # Fix connector import path
        code = _fix_connector_import(code, class_name)

        tests_dir = out_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        integ_path = tests_dir / "test_integration.py"
        integ_path.write_text(code, encoding="utf-8")
        await _emit(log_cb, "success", f"✅ tests/test_integration.py written ({len(code.splitlines())} lines, fields: {_cred_keys})")

        return {
            "status": "pass",
            "output": {
                "test_file": str(integ_path),
                "lines": len(code.splitlines()),
                "install_fields": _cred_keys,
            },
        }
    except Exception as exc:
        await _emit(log_cb, "error", f"Integration test generation failed: {exc}")
        return {"status": "fail", "output": str(exc)}


# ── Handler dispatch map ─────────────────────────────────────────────

async def handle_run_integration_tests(
    config: Dict[str, Any],
    context: Dict[str, Any],
    log_cb: LogCallback = None,
) -> Dict[str, Any]:
    """Integration tests are run from the UI with user-supplied credentials.
    The backend marks this step completed immediately so the build pipeline can continue.
    """
    await _emit(log_cb, "info", "Integration tests are run from the UI — skipping in backend pipeline.")
    return {"status": "pass", "output": "skipped — run from UI with real credentials"}


STEP_HANDLERS = {
    "install_deps": handle_install_deps,
    "configure_auth": handle_configure_auth,
    "scaffold_code": handle_scaffold_code,
    "generate_implementation_plan": handle_generate_implementation_plan,
    "write_connector": handle_write_connector,
    "smoke_test": handle_smoke_test,
    "syntax_check": handle_syntax_check,
    "generate_test_guidelines": handle_generate_test_guidelines,
    "implement_persistence": handle_implement_persistence,
    "write_tests": handle_write_tests,
    "run_tests": handle_run_tests,
    "write_integration_tests": handle_write_integration_tests,
    "run_integration_tests": handle_run_integration_tests,
    "generate_metadata": handle_generate_metadata,
    "setup_instructions": handle_setup_instructions,
}

# Fix handlers — keyed by step type, used by attempt_fix
FIX_HANDLERS = {
    "write_connector": handle_fix_connector,
    "write_tests": handle_fix_tests,           # structural test errors → fix test setup
    "run_tests": handle_fix_connector_for_tests,  # TDD: test failures → fix the CONNECTOR
    "smoke_test": handle_fix_smoke_test,       # import/instantiation errors → fast-fix then LLM
}

# Structural-only test fix (import errors, class name mismatches, collection errors)
FIX_HANDLERS_STRUCTURAL = {
    "write_tests": handle_fix_tests,
    "run_tests": handle_fix_tests,
}
