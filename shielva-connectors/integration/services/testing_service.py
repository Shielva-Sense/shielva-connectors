"""Integration Builder — Testing service.

Runs validation and pytest on generated connector code.
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from bson import ObjectId

from integration.core.config import settings
from integration.db.database import sessions_collection
from integration.schemas.models import SessionStatus, StepStatus, TestResults
from integration.services.code_quality import analyze_directory
from integration.services.validators import validate_all
from integration.services import failure_tracker

logger = structlog.get_logger(__name__)


def _resolve_output_dir(tenant_id: str, service_slug: str) -> Optional[Path]:
    """Find the generated code directory for a session.

    The on-disk layout is: {GENERATED_CODE_DIR}/{tenant_id}/{service_slug}_connector/
    Always returns an ABSOLUTE path so that subprocess calls with a different cwd
    don't accidentally double-up relative path segments.
    """
    out_dir = Path(settings.GENERATED_CODE_DIR).resolve() / tenant_id / f"{service_slug}_connector"
    return out_dir if out_dir.exists() else None


def _run_pytest_sync(
    tests_dir: Path,
    out_dir: Path,
    methods: List[str] | None = None,
    test_mode: str = "unit",
) -> Dict[str, Any]:
    """Run pytest synchronously — intended for asyncio.to_thread().

    Args:
        methods:   When provided, only run tests whose names contain any of the
                   method names (via pytest -k expression).  Dramatically speeds
                   up per-method runs — no point running 100+ tests when only one
                   method changed.
        test_mode: "unit" (default) — skip coverage instrumentation for speed.
                   "full" — run with --cov for the final coverage report.
    """
    import sysconfig as _sc, site as _st
    _site_pkgs = _sc.get_paths().get("purelib", "")
    _user_site = _st.getusersitepackages() if hasattr(_st, "getusersitepackages") else ""
    repo_root = Path(settings.GENERATED_CODE_DIR).resolve().parent
    python_path = os.pathsep.join(filter(None, [
        str(out_dir), str(out_dir.parent), _site_pkgs, _user_site, str(repo_root)
    ]))

    # Ensure conftest.py with asyncio_mode=auto
    conftest = tests_dir / "conftest.py"
    if not conftest.exists():
        conftest.write_text(
            "import pytest\n\n"
            "def pytest_configure(config):\n"
            "    config.addinivalue_line('markers', 'asyncio: mark test as async')\n",
            encoding="utf-8",
        )

    # Ensure pytest.ini with asyncio_mode=auto and per-test timeout
    pytest_ini = out_dir / "pytest.ini"
    if not pytest_ini.exists():
        pytest_ini.write_text("[pytest]\nasyncio_mode = auto\ntimeout = 60\n", encoding="utf-8")

    # Base command — use short tracebacks for speed; long tracebacks add significant I/O
    cmd = [
        sys.executable, "-m", "pytest",
        str(tests_dir),
        "-v", "--tb=short", "--no-header",
        f"--rootdir={out_dir}",
    ]

    # ── Method filter (-k) ───────────────────────────────────────────────────
    # Test functions are named test_{method}_* so filtering by method name is safe.
    # Using -k "install or authorize" means only those test functions are collected
    # and executed — everything else is deselected before pytest even imports them.
    if methods:
        k_expr = " or ".join(methods)
        cmd += ["-k", k_expr]

    # ── Coverage (full mode only) ────────────────────────────────────────────
    # Coverage instrumentation adds ~20-40% overhead — skip it for per-method unit runs.
    cov_json = out_dir / ".coverage_report.json"
    run_cov = (test_mode == "full")
    if run_cov:
        cmd += [
            f"--cov={out_dir}",
            "--cov-report=term-missing",
            f"--cov-report=json:{cov_json}",
            "--cov-config=/dev/null",
        ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(out_dir),
            env={**os.environ, "PYTHONPATH": python_path},
        )

        output = proc.stdout + proc.stderr

        # If coverage flags caused an error, retry without them
        if run_cov and (proc.returncode != 0) and (
            "unrecognized arguments" in output or "no module named pytest_cov" in output.lower()
        ):
            cmd_no_cov = [c for c in cmd if not c.startswith("--cov")]
            proc = subprocess.run(
                cmd_no_cov,
                capture_output=True, text=True, timeout=120,
                cwd=str(out_dir), env={**os.environ, "PYTHONPATH": python_path},
            )
            output = proc.stdout + proc.stderr
            run_cov = False

        passed = output.count(" PASSED")
        failed = output.count(" FAILED")
        skipped = output.count(" SKIPPED")
        errors = output.count("ERROR collecting") + output.count("ERROR tests/")

        details = []
        for line in output.split("\n"):
            if " PASSED" in line or " FAILED" in line or " SKIPPED" in line:
                parts = line.strip().split(" ")
                if len(parts) >= 2:
                    test_name = parts[0]
                    status = "passed" if "PASSED" in line else ("failed" if "FAILED" in line else "skipped")
                    details.append({"test": test_name, "status": status})
            elif "ERROR collecting" in line or ("ERROR " in line and "tests/" in line):
                details.append({"test": line.strip(), "status": "error"})

        # Parse per-test error messages from pytest failure blocks.
        # Format: "_ test_name _" header followed by "E   <error>" lines.
        import re as _re
        _failure_msgs: dict = {}
        _cur_test: str | None = None
        _cur_lines: list = []
        for _line in output.split("\n"):
            _hdr = _re.match(r"^_{5,}\s+(\S+)\s+_{5,}", _line)
            if _hdr:
                if _cur_test and _cur_lines:
                    _failure_msgs[_cur_test] = "\n".join(_cur_lines)
                _cur_test = _hdr.group(1).split("::")[-1]
                _cur_lines = []
            elif _cur_test:
                if _line.startswith("E ") or _line.startswith("E\t"):
                    _cur_lines.append(_line[2:].strip())
                elif _line.startswith("======"):
                    if _cur_test and _cur_lines:
                        _failure_msgs[_cur_test] = "\n".join(_cur_lines)
                    _cur_test = None
                    _cur_lines = []
        if _cur_test and _cur_lines:
            _failure_msgs[_cur_test] = "\n".join(_cur_lines)

        # Attach error message to each failing detail entry
        for _d in details:
            if _d["status"] == "failed":
                _fn = _d["test"].split("::")[-1] if "::" in _d["test"] else _d["test"]
                if _fn in _failure_msgs:
                    _d["message"] = _failure_msgs[_fn]

        # Parse coverage JSON only when we ran in full mode
        coverage_data: Dict[str, Any] = {}
        if run_cov and cov_json.exists():
            try:
                import json as _json
                raw_cov = _json.loads(cov_json.read_text())
                total = raw_cov.get("totals", {})
                coverage_data = {
                    "total_pct": round(total.get("percent_covered", 0), 1),
                    "covered_lines": total.get("covered_lines", 0),
                    "missing_lines": total.get("missing_lines", 0),
                    "num_statements": total.get("num_statements", 0),
                    "files": {
                        str(Path(fp).name): {
                            "pct": round(info.get("summary", {}).get("percent_covered", 0), 1),
                            "missing": info.get("missing_lines", []),
                        }
                        for fp, info in raw_cov.get("files", {}).items()
                        if "tests" not in fp
                    },
                }
                cov_json.unlink(missing_ok=True)
            except Exception:
                pass

        return {
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "errors": errors,
            "returncode": proc.returncode,
            "output": (output[:6000] + "\n...\n" + output[-2000:]) if len(output) > 8000 else output,
            "details": details,
            "coverage": coverage_data,
        }
    except subprocess.TimeoutExpired:
        return {
            "error": "TIMEOUT: pytest exceeded 120 seconds — too many or too slow tests",
            "passed": 0, "failed": 0, "skipped": 0, "details": [],
            "errors": 1,
            "root_cause": "tests_invalid",
        }
    except Exception as exc:
        return {"error": str(exc), "passed": 0, "failed": 0, "skipped": 0, "details": []}


async def run_tests(
    session_id: str,
    tenant_id: str,
    methods: List[str] | None = None,
    test_mode: str = "unit",
) -> Dict[str, Any]:
    """Run test suite on generated code: validation + pytest.

    Args:
        methods:   Only run tests whose names contain these method names (-k filter).
                   None / empty → run all tests.
        test_mode: "unit" (default) — fast, no coverage.
                   "full" — include --cov coverage report.
    """
    oid = ObjectId(session_id)
    # Use _id only — the gateway may inject a different tenant_id from JWT than
    # what is stored on the session, causing a silent 404.
    session = await sessions_collection().find_one({"_id": oid})
    if not session:
        raise ValueError(f"Session {session_id} not found")

    service = session.get("service", "")
    # Resolve tenant from stored session doc, not from the header
    stored_tenant = session.get("tenant_id") or tenant_id
    # service_slug is just the service name (provider prefix excluded — tenant_id
    # already provides isolation). Stored slug takes precedence for sessions that
    # were executed before this convention was established.
    service_slug = session.get("service_slug") or service.replace("-", "_").lower()

    out_dir = _resolve_output_dir(stored_tenant, service_slug)
    if not out_dir:
        raise ValueError(f"No generated files found (looked at {stored_tenant}/{service_slug})")

    # Update status to testing
    await sessions_collection().update_one(
        {"_id": oid},
        {"$set": {"status": SessionStatus.TESTING.value, "updated_at": datetime.utcnow()}},
    )

    results: Dict[str, Any] = {
        "validation": {},
        "pytest": {},
        "quality": {},
    }

    # ── 1. Static validation ─────────────────────────────────────────
    connector_path = out_dir / "connector.py"
    if connector_path.exists():
        code = connector_path.read_text(encoding="utf-8")
        validation = validate_all(code, str(connector_path))
        results["validation"] = validation
    else:
        results["validation"] = {"valid": False, "error": "connector.py not found"}

    # ── 2. Pytest (non-blocking via to_thread) ─────────────────────
    tests_dir = out_dir / "tests"
    passed = 0
    failed = 0
    skipped = 0
    details = []

    if tests_dir.exists() and list(tests_dir.glob("test_*.py")):
        pytest_result = await asyncio.to_thread(
            _run_pytest_sync, tests_dir, out_dir,
            methods or [], test_mode,
        )
        passed = pytest_result.get("passed", 0)
        failed = pytest_result.get("failed", 0)
        skipped = pytest_result.get("skipped", 0)
        details = pytest_result.get("details", [])
        results["pytest"] = pytest_result

        # Build method-level results for UI tracking
        from integration.services.step_executor import _build_method_test_map as _bmtm
        _test_file = tests_dir / "test_connector.py"
        _method_test_map = _bmtm(_test_file)
        method_results: Dict[str, Any] = {}
        for _method, _test_funcs in _method_test_map.items():
            _tests = []
            _mp = 0
            _mf = 0
            for _d in details:
                _fn = _d["test"].split("::")[-1] if "::" in _d["test"] else _d["test"]
                if _fn in _test_funcs:
                    _tests.append({"name": _fn, "status": _d["status"], "node_id": _d["test"]})
                    if _d["status"] == "passed":
                        _mp += 1
                    else:
                        _mf += 1
            method_results[_method] = {"tests": _tests, "passed": _mp, "failed": _mf}
    else:
        results["pytest"] = {"error": "No test files found", "passed": 0, "failed": 0}
        method_results = {}

    # ── 3. Code quality (non-blocking) ────────────────────────────
    quality = await asyncio.to_thread(analyze_directory, str(out_dir))
    results["quality"] = quality

    # ── Persist test results ─────────────────────────────────────────
    test_results = TestResults(
        passed=passed,
        failed=failed,
        skipped=skipped,
        coverage=None,
        details=details,
    )

    overall_pass = (
        results["validation"].get("valid", False)
        and failed == 0
        and passed > 0
    )

    # ── Failure tracking for run_tests step ──────────────────────────────
    provider = session.get("provider", "unknown")
    service_name = session.get("service", "unknown")
    # Find the run_tests step index from the plan
    plan_steps = session.get("plan", {}).get("steps", [])
    run_tests_step_index = next(
        (i for i, s in enumerate(plan_steps) if isinstance(s, dict) and s.get("type") == "run_tests"),
        len(plan_steps) - 1,  # fallback to last step
    )

    if overall_pass:
        asyncio.ensure_future(failure_tracker.resolve_failure(
            session_id=session_id,
            step_index=run_tests_step_index,
            provider=provider,
            service=service_name,
            tenant_id=stored_tenant,
        ))
    else:
        pytest_out = results.get("pytest", {})
        validation_out = results.get("validation", {})
        error_summary_parts = []
        if not validation_out.get("valid"):
            error_summary_parts.append(f"Validation failed: {validation_out.get('error', 'see details')}")
        if failed > 0:
            error_summary_parts.append(f"{failed} test(s) failed, {passed} passed")
        elif passed == 0 and not pytest_out.get("error"):
            error_summary_parts.append("No tests ran (0 passed)")
        if pytest_err := pytest_out.get("error"):
            error_summary_parts.append(pytest_err)
        asyncio.ensure_future(failure_tracker.create_failure(
            session_id=session_id,
            step_index=run_tests_step_index,
            step_type="run_tests",
            provider=provider,
            service=service_name,
            tenant_id=stored_tenant,
            error_summary="; ".join(error_summary_parts) or "Test suite failed",
            full_output=pytest_out.get("output", "")[:6000],
        ))

    # Store full results dict (including validation, pytest, quality) so session restore
    # can display the complete test results without needing to re-run tests.
    full_test_results = {
        **test_results.model_dump(mode="json"),
        "validation": results.get("validation", {}),
        "pytest": results.get("pytest", {}),
        "quality": results.get("quality", {}),
        "overall_pass": overall_pass,
        "method_results": method_results,
    }

    # Build the update — always persist test results + session status
    _set_fields: dict = {
        "test_results": full_test_results,
        "status": SessionStatus.COMPLETED.value if overall_pass else SessionStatus.FAILED.value,
        "updated_at": datetime.utcnow(),
    }

    # When tests pass, mark write_tests and run_tests plan steps as completed so the
    # UI step indicator turns green (it reads plan.steps.N.status).
    if overall_pass:
        for _i, _s in enumerate(plan_steps):
            if isinstance(_s, dict) and _s.get("type") in ("write_tests", "run_tests"):
                _set_fields[f"plan.steps.{_i}.status"] = StepStatus.COMPLETED.value

    await sessions_collection().update_one(
        {"_id": oid},
        {"$set": _set_fields},
    )

    logger.info(
        "tests.completed",
        session_id=session_id,
        passed=passed,
        failed=failed,
        validation_ok=results["validation"].get("valid", False),
    )

    return {
        "session_id": session_id,
        "overall_pass": overall_pass,
        "method_results": method_results,
        **results,
    }
