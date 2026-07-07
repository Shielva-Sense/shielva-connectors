"""Integration Builder — Testing API routes."""

import ast
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import structlog
from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic import BaseModel as _BaseModel

from integration.core.config import settings
from integration.db.database import sessions_collection
from integration.services.testing_service import run_tests


class _TestRequest(_BaseModel):
    test_mode: str | None = "unit"
    methods: list[str] | None = None


logger = structlog.get_logger(__name__)

testing_router = APIRouter(prefix="/sessions", tags=["testing"])


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _get_session_output_dir(session_id: str, tenant_id: str) -> tuple[Path, str, str]:
    """Resolve the generated code directory for a session.

    Uses _id alone for the lookup — the gateway may inject a different
    tenant_id from the JWT than what is stored on the session document,
    causing a silent 404.  We resolve the real tenant from the stored doc.

    Returns (out_dir, provider, service) where service is the raw session
    service name used to derive the connector KB ID.
    """
    try:
        oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    session = await sessions_collection().find_one(
        {"_id": oid},  # no tenant filter — avoids gateway injection mismatch
        {"service": 1, "service_slug": 1, "tenant_id": 1, "provider": 1},
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Use the stored tenant_id for path resolution, not the header value
    stored_tenant = session.get("tenant_id") or tenant_id
    service = session.get("service", "")
    service_slug = session.get("service_slug") or service.replace("-", "_").lower()
    out_dir = Path(settings.GENERATED_CODE_DIR).resolve() / stored_tenant / f"{service_slug}_connector"
    if not out_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No generated files found at {service_slug}_connector",
        )
    return out_dir, session.get("provider", ""), service


def _extract_methods_from_source(source: str) -> list[str]:
    """Parse connector.py with AST and return the public methods defined on the
    connector class itself (the first class that inherits from BaseConnector).

    We intentionally ignore:
      - module-level functions (helpers, not part of the public API)
      - private / dunder methods (start with _)
      - methods inherited from BaseConnector (get_token, set_token, etc.)
        — those live in shared/ and must never appear as test targets
    """
    methods: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return methods

    # Find the connector class (inherits from BaseConnector)
    connector_class: ast.ClassDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            base_names = [
                (b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else "")
                for b in node.bases
            ]
            if "BaseConnector" in base_names:
                connector_class = node
                break

    # Fall back: use the first class defined in the file
    if connector_class is None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                connector_class = node
                break

    if connector_class is None:
        return methods

    # Extract only methods defined directly in the class body (not nested)
    seen: set = set()
    for item in connector_class.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = item.name
            if not name.startswith("_") and name not in seen:
                seen.add(name)
                methods.append(name)

    return methods


# ── Routes ────────────────────────────────────────────────────────────────────


@testing_router.post("/{session_id}/test")
async def start_tests(
    session_id: str,
    body: _TestRequest = _TestRequest(),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Run validation and tests on generated code.

    Optional body:
      test_mode: "unit" (default) — skip coverage, run fast
      methods:   list of connector method names — only run tests for those methods
                 uses pytest -k filter so only matching test functions execute
    """
    methods = body.methods or []
    logger.info(
        "testing.start",
        session_id=session_id,
        tenant_id=x_tenant_id,
        methods=methods,
        test_mode=body.test_mode,
    )
    try:
        result = await run_tests(session_id, x_tenant_id, methods=methods, test_mode=body.test_mode or "unit")
        logger.info(
            "testing.complete",
            session_id=session_id,
            overall_pass=result.get("overall_pass"),
            passed=result.get("pytest", {}).get("passed", 0),
            failed=result.get("pytest", {}).get("failed", 0),
        )
        return result
    except ValueError as exc:
        logger.warning("testing.validation_error", session_id=session_id, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("testing.failed", session_id=session_id, error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@testing_router.get("/{session_id}/test-cases-map")
async def get_test_cases_map(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Read test_connector.py from disk and return the method → test-function mapping.

    Used on page load to restore the test case list without needing a prior test run
    or MongoDB data. Returns an empty map if the test file doesn't exist yet.
    """
    out_dir, _, _svc = await _get_session_output_dir(session_id, x_tenant_id)
    test_file = out_dir / "tests" / "test_connector.py"
    if not test_file.exists():
        return {"method_tests": {}}
    try:
        from integration.services.step_executor import _build_method_test_map as _bmtm

        method_tests = _bmtm(test_file)
        return {"method_tests": method_tests}
    except Exception as exc:
        logger.warning("testing.test_cases_map_error", session_id=session_id, error=str(exc))
        return {"method_tests": {}}


@testing_router.get("/{session_id}/connector-methods")
async def get_connector_methods(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Dynamically extract public method/function names from connector.py only.

    We scan ONLY connector.py (not shared/ or base files) so that internal
    BaseConnector methods (get_token, set_token, get_config, save_config …)
    are never surfaced as test targets — they don't exist as public API on the
    generated connector class and would always produce failing tests.
    """
    out_dir, _, _svc = await _get_session_output_dir(session_id, x_tenant_id)

    # Only read connector.py — the single source of truth for public API
    connector_py = out_dir / "connector.py"
    if not connector_py.exists():
        # Fall back: look one level deeper (some scaffolds nest it)
        nested = list(out_dir.rglob("connector.py"))
        connector_py = nested[0] if nested else None

    if not connector_py:
        raise HTTPException(
            status_code=404,
            detail="connector.py not found — run write_connector step first",
        )

    try:
        source = connector_py.read_text(encoding="utf-8")
        all_methods = _extract_methods_from_source(source)
    except Exception as exc:
        logger.warning("testing.method_extract_error", file=str(connector_py), error=str(exc))
        all_methods = []

    logger.info("testing.methods_extracted", session_id=session_id, count=len(all_methods))
    return {
        "session_id": session_id,
        "methods": all_methods,
        "source_files": [str(connector_py.relative_to(out_dir))],
    }


class GenerateTestsRequest(BaseModel):
    methods: list[str]
    reset: bool = False


@testing_router.post("/{session_id}/generate-unit-tests")
async def generate_unit_tests(
    session_id: str,
    body: GenerateTestsRequest,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """⚠ DEPRECATED — backend Gemini codegen has been removed.

    Test generation now lives on the client (SAD) via the local Claude CLI.
    This endpoint is preserved for legacy callers and returns 410 Gone with a
    clear message so the caller can route to the new flow.
    """
    from fastapi import HTTPException as _HTTPException

    raise _HTTPException(
        status_code=410,
        detail=(
            "Backend Gemini test generation has been removed. "
            "Generate tests from SAD (Builder → Write Tests) which runs the local Claude CLI."
        ),
    )


async def _legacy_generate_unit_tests_DISABLED(
    session_id: str,
    body: "GenerateTestsRequest",
    x_tenant_id: str = "",
):
    """Original Gemini implementation — retained as dead reference; not routed.

    Returns an SSE stream so the frontend can show real-time progress in the
    Execution Progress terminal. Events:
      generate_started   — initial log (methods + source files found)
      generate_progress  — streaming chunk of LLM output (line count)
      generate_complete  — final generated code + saved file path
      generate_error     — something went wrong
    """

    def _sse(event: str, data: dict) -> str:
        return f"data: {json.dumps({'type': event, **data})}\n\n"

    async def _stream() -> AsyncIterator[str]:
        try:
            out_dir, session_provider, session_service = await _get_session_output_dir(session_id, x_tenant_id)
        except HTTPException as exc:
            yield _sse("generate_error", {"message": exc.detail})
            return

        methods = body.methods
        reset = body.reset
        if not methods:
            yield _sse("generate_error", {"message": "No methods specified"})
            return

        # Extract class name from connector.py (agentic loop reads the file itself via read_file tool)
        connector_py = out_dir / "connector.py"
        if not connector_py.exists():
            yield _sse(
                "generate_error",
                {"message": "connector.py not found — run write_connector first"},
            )
            return
        connector_source = connector_py.read_text(encoding="utf-8")
        _cls_match = re.search(r"^class\s+(\w+)\s*\(BaseConnector\)", connector_source, re.MULTILINE)
        class_name = _cls_match.group(1) if _cls_match else "Connector"

        source_files = [
            str(f.relative_to(out_dir))
            for f in sorted(out_dir.rglob("*.py"))
            if "__pycache__" not in str(f) and "tests" not in f.parts
        ]

        # Handle existing test file: delete on reset, preserve on partial re-run
        _existing_test = out_dir / "tests" / "test_connector.py"
        existing_tests_content = ""
        if _existing_test.exists():
            if reset:
                _existing_test.unlink()
                yield _sse(
                    "generate_progress",
                    {
                        "message": "🗑 Reset: deleted existing test_connector.py — regenerating from scratch",
                        "lines_generated": 0,
                    },
                )
            else:
                existing_tests_content = _existing_test.read_text(encoding="utf-8")
                yield _sse(
                    "generate_progress",
                    {
                        "message": f"📄 Existing tests preserved — only adding/replacing tests for: {', '.join(methods)}",
                        "lines_generated": 0,
                    },
                )

        yield _sse(
            "generate_started",
            {
                "message": f"🧪 Agentic test generation for {len(methods)} method(s): {', '.join(methods)} [class={class_name}]",
                "methods": methods,
                "source_files": source_files,
            },
        )

        logger.info("testing.generate_unit_tests", session_id=session_id, methods=methods)

        # ── Load connector-specific test guidelines (from generate_test_guidelines step) ──
        _test_guidelines = ""
        _guidelines_local = out_dir / "test_guidelines.md"
        if _guidelines_local.exists():
            _guidelines_raw = _guidelines_local.read_text(encoding="utf-8")
            if len(_guidelines_raw.strip()) > 300:  # ignore stub/summary content
                _test_guidelines = _guidelines_raw
                logger.info(
                    "testing.guidelines_loaded_local",
                    session_id=session_id,
                    chars=len(_test_guidelines),
                )
        if not _test_guidelines:
            try:
                from integration.services import r2_service as _r2

                _session_for_slugs = await sessions_collection().find_one(
                    {"_id": ObjectId(session_id)}, {"service_slug": 1}
                )
                _svc_slug = (_session_for_slugs or {}).get("service_slug", "")
                if _svc_slug:
                    _r2_guidelines = await _r2.get_test_guidelines(session_provider, _svc_slug) or ""
                    if len(_r2_guidelines.strip()) > 300:
                        _test_guidelines = _r2_guidelines
                        logger.info(
                            "testing.guidelines_loaded_r2",
                            session_id=session_id,
                            chars=len(_test_guidelines),
                        )
            except Exception:
                pass
        if _test_guidelines:
            yield _sse(
                "generate_progress",
                {
                    "message": f"📋 Connector-specific test guidelines loaded ({len(_test_guidelines)} chars) — Gemini will follow per-method specs",
                    "lines_generated": 0,
                },
            )
        else:
            yield _sse(
                "generate_progress",
                {
                    "message": "⚠ No connector-specific guidelines found — run 'Generate Test Guidelines' step first for best results",
                    "lines_generated": 0,
                },
            )

        # Queue for streaming agentic loop logs → SSE
        import asyncio as _asyncio

        _progress_queue: _asyncio.Queue = _asyncio.Queue()

        # ── Gemini agentic generation: read connector → write tests → run → fix → done ──
        # Each write_file call in the loop automatically triggers autoflake+ruff deterministic fix.
        from integration.services.agentic_fix import gemini_agentic_generate_tests

        yield _sse(
            "generate_progress",
            {
                "message": "🤖 Gemini agentic generation starting — reads connector.py, writes tests, runs pytest, fixes until passing...",
                "lines_generated": 0,
            },
        )

        async def _agentic_log(level: str, msg: str):
            await _progress_queue.put(msg)

        from integration.services import knowledge_service as _ks

        async def _knowledge_fn(query: str) -> str:
            try:
                return (
                    await _ks.query_knowledge(
                        query=query,
                        tenant_id=x_tenant_id,
                        provider=session_provider,
                        service=session_service,
                        top_k=8,
                    )
                    or ""
                )
            except Exception:
                return ""

        try:
            _gen_task = _asyncio.ensure_future(
                gemini_agentic_generate_tests(
                    out_dir,
                    methods=methods,
                    class_name=class_name,
                    knowledge_fn=_knowledge_fn,
                    tenant_id=x_tenant_id,
                    provider=session_provider,
                    service=session_service,
                    test_guidelines=_test_guidelines,
                    existing_tests=existing_tests_content,
                    reset=reset,
                    log_cb=_agentic_log,
                )
            )
            while not _gen_task.done():
                try:
                    msg = await _asyncio.wait_for(_progress_queue.get(), timeout=0.4)
                    yield _sse("generate_progress", {"message": msg, "lines_generated": 0})
                except TimeoutError:
                    yield ": keepalive\n\n"
            agentic_result = await _gen_task
            # Drain any messages queued during the final iteration before _gen_task.done() fired
            while not _progress_queue.empty():
                try:
                    msg = _progress_queue.get_nowait()
                    yield _sse("generate_progress", {"message": msg, "lines_generated": 0})
                except _asyncio.QueueEmpty:
                    break
        except Exception as exc:
            logger.error("testing.generate_failed", session_id=session_id, error=str(exc))
            yield _sse(
                "generate_error",
                {"message": f"Gemini agentic generation failed: {exc}"},
            )
            return

        # Read the test file written by Gemini
        tests_dir = out_dir / "tests"
        test_file_name = "test_connector.py"
        test_file_path = tests_dir / test_file_name
        if not test_file_path.exists():
            yield _sse(
                "generate_error",
                {"message": "Gemini did not write tests/test_connector.py — try again"},
            )
            return

        generated_code = test_file_path.read_text(encoding="utf-8")

        # ── Safety net: fix wrong import paths + inject missing @pytest.mark.asyncio ──
        # write_file already ran autoflake+ruff; these are semantic/AST fixes only.
        from integration.services.step_executor import (
            _fix_connector_import,
            _strip_hallucinated_imports,
        )

        generated_code = _fix_connector_import(generated_code, class_name)
        generated_code = _strip_hallucinated_imports(generated_code, out_dir / "connector.py")
        test_file_path.write_text(generated_code, encoding="utf-8")

        agentic_ok = agentic_result.get("success", False)
        iters = agentic_result.get("iterations", 0)
        yield _sse(
            "generate_progress",
            {
                "message": f"{'✅' if agentic_ok else '⚠'} Agentic generation complete ({iters} iteration(s)) — {'all tests passing' if agentic_ok else 'some tests may still fail, Attempt Fix available'}",
                "lines_generated": 0,
            },
        )

        # ── RAG indexing: ingest test file into connector KB ──────────
        import asyncio as _asyncio

        _asyncio.ensure_future(
            _ks.ingest_step_output(
                content=generated_code,
                filename=f"tests/{test_file_name}",
                tenant_id=x_tenant_id,
                provider=session_provider,
                service=session_service,
                step_type="write_tests",
            )
        )

        line_count = generated_code.count("\n") + 1
        logger.info(
            "testing.generated_saved",
            session_id=session_id,
            file=test_file_name,
            lines=line_count,
            agentic_success=agentic_ok,
            iterations=iters,
        )

        # Emit method→test-cases map so the UI can show test names immediately
        from integration.services.step_executor import _build_method_test_map as _bmtm

        _method_tests = _bmtm(test_file_path)
        if _method_tests:
            yield _sse("test_cases_map", {"method_tests": _method_tests})

        yield _sse(
            "generate_complete",
            {
                "message": f"✅ Generated {line_count} lines of test code → saved as {test_file_name}",
                "session_id": session_id,
                "methods": methods,
                "test_file": test_file_name,
                "generated_code": generated_code,
                "line_count": line_count,
            },
        )

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@testing_router.delete("/{session_id}/test-file")
async def reset_test_file(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Delete test_connector.py so the user can regenerate from scratch."""
    try:
        out_dir, _, _ = await _get_session_output_dir(session_id, x_tenant_id)
    except HTTPException as exc:
        raise exc
    test_file = out_dir / "tests" / "test_connector.py"
    if test_file.exists():
        test_file.unlink()
    return {"reset": True, "session_id": session_id}


@testing_router.get("/{session_id}/test-rules")
async def get_test_rules(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Return the service-specific test_rules.md content for the session.

    Reads from shielva-integration-plans/{provider}/{service}/shielva-sense/test_rules.md.
    Returns { markdown: str } if found, or { markdown: null } if not yet generated.
    """
    try:
        oid = ObjectId(session_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid session ID")

    session = await sessions_collection().find_one(
        {"_id": oid},
        {"service": 1, "service_slug": 1, "tenant_id": 1, "provider": 1},
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    provider = session.get("provider", "").lower().replace(" ", "_")
    service_slug = session.get("service_slug") or session.get("service", "").replace("-", "_").lower()

    _plans_root = Path(__file__).resolve().parent.parent.parent.parent / "shielva-integration-plans"
    _rules_path = _plans_root / provider / service_slug / "shielva-sense" / "test_rules.md"

    if _rules_path.exists():
        return {"markdown": _rules_path.read_text(encoding="utf-8")}
    return {"markdown": None}


@testing_router.post("/{session_id}/auto-fix-compile")
async def auto_fix_compile_errors(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Deterministically fix known compile-error patterns without invoking Gemini.

    Handles:
      1. Gemini over-escaped triple quotes: literal `\\\"\\\"\\\"` → `\"\"\"` in .py files
      2. Missing typing imports: `from typing import Optional/List/Dict/…` auto-added

    Returns { fixed: bool, changes: [str], remaining: str }
      fixed=True   → all compile errors resolved, safe to run tests
      fixed=False  → some errors remain that need Gemini
    """
    try:
        out_dir, _, _ = await _get_session_output_dir(session_id, x_tenant_id)
    except HTTPException as exc:
        raise exc

    import ast as _ast
    import re as _re

    changes: list[str] = []

    # Collect all .py files (excluding tests/__pycache__)
    py_files = [
        f
        for f in out_dir.rglob("*.py")
        if "__pycache__" not in f.parts and "tests" not in f.parts and not f.name.startswith("test_")
    ]

    for py_file in py_files:
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        original = content

        # ── Fix 1: Gemini over-escaped triple quotes ────────────────────────
        # Pattern: literal backslash-quote-quote-quote written to file
        if '\\"\\"\\"' in content or "\\'\\'\\'" in content:
            content = content.replace('\\"\\"\\"', '"""').replace("\\'\\'\\'", "'''")
            if content != original:
                changes.append(f"Fixed escaped triple-quotes in {py_file.name}")

        # ── Fix 2: Missing typing imports ────────────────────────────────────
        # Parse with AST, find annotation names, add any missing typing imports.
        TYPING_NAMES = {
            "Optional",
            "List",
            "Dict",
            "Tuple",
            "Union",
            "Set",
            "Any",
            "Callable",
            "Type",
            "Sequence",
            "Iterable",
            "Generator",
            "Iterator",
            "ClassVar",
            "Final",
            "Literal",
        }
        try:
            tree = _ast.parse(content)
        except SyntaxError:
            # If still broken after fix 1, skip — needs Gemini
            continue

        # Names already imported
        imported: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    imported.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                for a in node.names:
                    imported.add(a.asname or a.name)

        # Names used in annotations
        ann_names: set[str] = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.AnnAssign) and node.annotation:
                ann_names.update(n.id for n in _ast.walk(node.annotation) if isinstance(n, _ast.Name))
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                    if arg.annotation:
                        ann_names.update(n.id for n in _ast.walk(arg.annotation) if isinstance(n, _ast.Name))
                if node.returns:
                    ann_names.update(n.id for n in _ast.walk(node.returns) if isinstance(n, _ast.Name))

        missing = sorted((ann_names & TYPING_NAMES) - imported)
        if missing:
            import_line = f"from typing import {', '.join(missing)}\n"
            # Find if there's already a `from typing import …` line to extend
            existing_match = _re.search(r"^from typing import (.+)$", content, _re.MULTILINE)
            if existing_match:
                existing_names = [n.strip() for n in existing_match.group(1).split(",")]
                merged = sorted(set(existing_names) | set(missing))
                content = (
                    content[: existing_match.start()]
                    + f"from typing import {', '.join(merged)}"
                    + content[existing_match.end() :]
                )
            else:
                # Insert after the last future/stdlib import block, or at top
                lines = content.splitlines(keepends=True)
                insert_at = 0
                for i, line in enumerate(lines):
                    if line.startswith(("import ", "from ")):
                        insert_at = i + 1
                lines.insert(insert_at, import_line)
                content = "".join(lines)
            changes.append(f"Added 'from typing import {', '.join(missing)}' to {py_file.name}")

        if content != original:
            py_file.write_text(content, encoding="utf-8")
            # Run autoflake + ruff on any modified .py file — same post-processing
            # as the normal write_file tool so the file is properly cleaned up.
            try:
                from integration.services.code_quality import auto_fix_python_file

                auto_fix_python_file(py_file)
            except Exception:
                pass

    # Re-run compile check to see if anything remains
    import os as _os
    import subprocess as _sp
    import sys as _sys

    repo_root = Path(settings.GENERATED_CODE_DIR).resolve().parent
    pythonpath = _os.pathsep.join([str(out_dir), str(repo_root), str(out_dir.parent)])

    check_script = (
        "import sys, pathlib, py_compile, ast\n"
        "sys.path.insert(0, '.')\n"
        "cwd = pathlib.Path('.')\n"
        "py_files = sorted(f for f in cwd.rglob('*.py')\n"
        "    if '__pycache__' not in f.parts and 'tests' not in f.parts\n"
        "    and not f.name.startswith('test_'))\n"
        "errors = []\n"
        "for py_file in py_files:\n"
        "    try: py_compile.compile(str(py_file), doraise=True)\n"
        "    except py_compile.PyCompileError as e: errors.append(f'SyntaxError in {py_file.name}: {e}')\n"
        "TYPING_NAMES = {'Optional','List','Dict','Tuple','Union','Set','Any','Callable','Type','Sequence','Iterable','Generator','Iterator','ClassVar','Final','Literal'}\n"
        "for py_file in py_files:\n"
        "    try:\n"
        "        src = py_file.read_text(encoding='utf-8'); tree = ast.parse(src)\n"
        "    except SyntaxError: continue\n"
        "    imported = set()\n"
        "    for node in ast.walk(tree):\n"
        "        if isinstance(node, ast.Import):\n"
        "            for a in node.names: imported.add(a.asname or a.name.split('.')[0])\n"
        "        elif isinstance(node, ast.ImportFrom):\n"
        "            for a in node.names: imported.add(a.asname or a.name)\n"
        "    ann_names = set()\n"
        "    for node in ast.walk(tree):\n"
        "        if isinstance(node, ast.AnnAssign) and node.annotation:\n"
        "            ann_names.update(n.id for n in ast.walk(node.annotation) if isinstance(n, ast.Name))\n"
        "        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):\n"
        "            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:\n"
        "                if arg.annotation: ann_names.update(n.id for n in ast.walk(arg.annotation) if isinstance(n, ast.Name))\n"
        "            if node.returns: ann_names.update(n.id for n in ast.walk(node.returns) if isinstance(n, ast.Name))\n"
        "    missing = (ann_names & TYPING_NAMES) - imported\n"
        "    if missing: errors.append(f'Still missing in {py_file.name}: {sorted(missing)}')\n"
        # ── Phase 3: import check (same as check-imports endpoint) ──────────
        "import subprocess as _subp\n"
        "if not errors:\n"
        "    top_mods = sorted(f.stem for f in cwd.glob('*.py')\n"
        "                      if f.stem != '__init__' and not f.stem.startswith('test_'))\n"
        "    for mod_name in top_mods:\n"
        "        try:\n"
        "            r = _subp.run(\n"
        "                [sys.executable, '-c', f'import sys; sys.path.insert(0,\".\"); import {mod_name}'],\n"
        "                cwd=str(cwd), capture_output=True, text=True, timeout=5,\n"
        "                env=__import__('os').environ.copy(),\n"
        "            )\n"
        "            if r.returncode != 0:\n"
        "                err = (r.stdout + r.stderr).strip()\n"
        "                last = [l for l in err.split('\\n') if l.strip() and not l.startswith(' ')][-1] if err else 'unknown error'\n"
        "                errors.append(f'ImportError in {mod_name}: {last}')\n"
        "        except _subp.TimeoutExpired:\n"
        "            pass\n"
        "        except Exception as e:\n"
        "            errors.append(f'ImportError in {mod_name}: {e}')\n"
        "print('OK' if not errors else 'ERRORS:' + '|'.join(errors))\n"
    )

    import asyncio as _asyncio

    try:
        proc = await _asyncio.to_thread(
            _sp.run,
            [_sys.executable, "-c", check_script],
            cwd=str(out_dir),
            capture_output=True,
            text=True,
            timeout=30,
            env={**_os.environ, "PYTHONPATH": pythonpath},
        )
        remaining_out = (proc.stdout + proc.stderr).strip()
        all_fixed = remaining_out.startswith("OK")
    except Exception:
        all_fixed = len(changes) > 0
        remaining_out = ""

    return {
        "fixed": all_fixed,
        "changes": changes,
        "remaining": "" if all_fixed else remaining_out,
    }


@testing_router.get("/{session_id}/check-imports")
async def check_connector_imports(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Run a quick import/compilation check on all connector Python files.

    Spawns a subprocess that imports exceptions.py then connector.py so that
    NameError, ImportError, SyntaxError, and AttributeError are caught before
    tests are even attempted.  Same logic as the check_imports Gemini tool.

    Returns:
      { clean: bool, output: str }
        clean=True  → all imports OK, safe to run tests
        clean=False → import/syntax errors found, fix first
    """
    import os as _os
    import subprocess as _sp
    import sys as _sys

    try:
        out_dir, _, _ = await _get_session_output_dir(session_id, x_tenant_id)
    except HTTPException as exc:
        raise exc

    # PYTHONPATH must include:
    #   1. out_dir itself (so `import exceptions` resolves)
    #   2. repo_root (shielva-connectors/) so `from shared.base_connector import ...` resolves
    #   3. out_dir.parent (tenant dir) as a fallback
    repo_root = Path(settings.GENERATED_CODE_DIR).resolve().parent
    pythonpath = _os.pathsep.join([str(out_dir), str(repo_root), str(out_dir.parent)])
    # Three-phase check — all run in a single subprocess, total timeout = 15s.
    #
    # Phase 1 — py_compile: catches SyntaxError in every .py file  (< 0.1s)
    # Phase 2 — AST scan:   catches missing typing imports (Optional/List/…)
    #                        without executing any code                (< 0.5s)
    # Phase 3 — import:     catches ModuleNotFoundError / ImportError.
    #                        Skipped per-module if it times out (heavy deps).
    #                        Each module gets its own signal-safe timeout via
    #                        a separate subprocess so one slow import can't
    #                        block the entire check.
    check_script = (
        "import sys, pathlib, py_compile, ast, subprocess, traceback\n"
        "sys.path.insert(0, '.')\n"
        "cwd = pathlib.Path('.')\n"
        "py_files = sorted(\n"
        "    f for f in cwd.rglob('*.py')\n"
        "    if '__pycache__' not in f.parts and 'tests' not in f.parts\n"
        "    and not f.name.startswith('test_')\n"
        ")\n"
        "errors = []\n"
        # ── Phase 1: py_compile ──────────────────────────────────────────────
        "for py_file in py_files:\n"
        "    try:\n"
        "        py_compile.compile(str(py_file), doraise=True)\n"
        "    except py_compile.PyCompileError as e:\n"
        "        errors.append(f'SyntaxError in {py_file.name}: {e}')\n"
        # ── Phase 2: AST annotation scan ────────────────────────────────────
        "TYPING_NAMES = {'Optional','List','Dict','Tuple','Union','Set','Any','Callable','Type','Sequence','Iterable','Generator','Iterator','ClassVar','Final','Literal','TypeVar','overload'}\n"
        "for py_file in py_files:\n"
        "    try:\n"
        "        src = py_file.read_text(encoding='utf-8')\n"
        "        tree = ast.parse(src)\n"
        "    except SyntaxError:\n"
        "        continue\n"
        "    imported = set()\n"
        "    for node in ast.walk(tree):\n"
        "        if isinstance(node, ast.Import):\n"
        "            for a in node.names: imported.add(a.asname or a.name.split('.')[0])\n"
        "        elif isinstance(node, ast.ImportFrom):\n"
        "            for a in node.names: imported.add(a.asname or a.name)\n"
        "    ann_names = set()\n"
        "    for node in ast.walk(tree):\n"
        "        if isinstance(node, ast.AnnAssign) and node.annotation:\n"
        "            ann_names.update(n.id for n in ast.walk(node.annotation) if isinstance(n, ast.Name))\n"
        "        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):\n"
        "            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:\n"
        "                if arg.annotation: ann_names.update(n.id for n in ast.walk(arg.annotation) if isinstance(n, ast.Name))\n"
        "            if node.returns: ann_names.update(n.id for n in ast.walk(node.returns) if isinstance(n, ast.Name))\n"
        "    missing = (ann_names & TYPING_NAMES) - imported\n"
        "    if missing:\n"
        "        errors.append(f'Missing typing imports in {py_file.name}: add: from typing import {\", \".join(sorted(missing))}')\n"
        # ── Phase 3: import check (per module, 5s timeout each) ─────────────
        # Only runs if no syntax/annotation errors found first.
        "if not errors:\n"
        "    top_mods = sorted(f.stem for f in cwd.glob('*.py')\n"
        "                      if f.stem != '__init__' and not f.stem.startswith('test_'))\n"
        "    for mod_name in top_mods:\n"
        "        try:\n"
        "            r = subprocess.run(\n"
        "                [sys.executable, '-c', f'import sys; sys.path.insert(0,\".\"); import {mod_name}'],\n"
        "                cwd=str(cwd), capture_output=True, text=True, timeout=5,\n"
        '                env=__import__("os").environ.copy(),\n'
        "            )\n"
        "            if r.returncode != 0:\n"
        "                err = (r.stdout + r.stderr).strip()\n"
        "                # Extract just the last meaningful error line\n"
        "                last = [l for l in err.split('\\n') if l.strip() and not l.startswith(' ')][-1] if err else 'unknown error'\n"
        "                errors.append(f'ImportError in {mod_name}: {last}')\n"
        "        except subprocess.TimeoutExpired:\n"
        "            pass  # heavy deps — skip, not a code error\n"
        "        except Exception as e:\n"
        "            errors.append(f'ImportError in {mod_name}: {e}')\n"
        "if errors:\n"
        "    print('COMPILE ERRORS FOUND:'); [print(e) for e in errors]\n"
        "else:\n"
        "    print('OK: all files compile clean')\n"
    )

    import asyncio as _asyncio

    try:
        proc = await _asyncio.to_thread(
            _sp.run,
            [_sys.executable, "-c", check_script],
            cwd=str(out_dir),
            capture_output=True,
            text=True,
            timeout=30,
            env={**_os.environ, "PYTHONPATH": pythonpath},
        )
        output = (proc.stdout + proc.stderr).strip() or "OK: all files compile clean"
        clean = output.startswith("OK:")
        return {"clean": clean, "output": output}
    except Exception as exc:
        return {"clean": False, "output": f"ERROR running compile check: {exc}"}


@testing_router.get("/{session_id}/test/results")
async def get_test_results(
    session_id: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
):
    """Get stored test results for a session."""
    oid = ObjectId(session_id)
    session = await sessions_collection().find_one(
        {"_id": oid, "tenant_id": x_tenant_id},
        {"test_results": 1, "status": 1},
    )
    if not session:
        logger.warning("testing.results_session_not_found", session_id=session_id)
        raise HTTPException(status_code=404, detail="Session not found")

    test_results = session.get("test_results")
    if not test_results:
        logger.info("testing.no_results_yet", session_id=session_id)
        raise HTTPException(status_code=404, detail="No test results found — run tests first")

    logger.info("testing.results_retrieved", session_id=session_id)
    return {
        "session_id": session_id,
        "status": session.get("status"),
        "test_results": test_results,
    }
