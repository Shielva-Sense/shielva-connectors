"""
End-to-end integration test for the "Generate Test Cases" workflow.

Flow tested (mirrors exactly what the UI does):
  Step 1 → GET  /sessions/{id}/connector-methods   — extract real public methods
  Step 2 → POST /sessions/{id}/generate-unit-tests  — Gemini generates test code (SSE)
  Step 3 → POST /sessions/{id}/test                 — pytest runs the generated file

All calls hit the real running server (http://localhost:8055) with real Gemini.
No mocks. No stubs.

Run:
    pytest tests/test_generate_workflow.py -v -s
"""

import json
import time

import httpx
import pytest

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL = "https://localhost:8055"
SESSION_ID = "69bf9e284c6f65db898a109c"  # AWS Lambda session
TENANT_ID = "shielva-sense"
HEADERS = {"X-Tenant-ID": TENANT_ID}

# Timeouts — Gemini can take up to 90 s for a full generation
GENERATE_TIMEOUT = 180  # seconds for SSE stream
RUN_TEST_TIMEOUT = 300  # seconds for pytest run


# ── Helpers ───────────────────────────────────────────────────────────────────


def _parse_sse_events(raw: str) -> list[dict]:
    """Parse raw SSE text into a list of event dicts."""
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass  # keepalive lines ": keepalive" — skip
    return events


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestGenerateWorkflow:
    """Full end-to-end: connector-methods → generate-unit-tests (Gemini) → run tests."""

    # ── Step 1: connector-methods ─────────────────────────────────────────────

    def test_step1_connector_methods_returns_valid_list(self):
        """Step 1 — GET connector-methods must return the real public methods."""
        resp = httpx.get(
            f"{BASE_URL}/sessions/{SESSION_ID}/connector-methods",
            headers=HEADERS,
            timeout=15,
            verify=False,  # noqa: S501 — test client against local dev server
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        data = resp.json()
        print(f"\n[Step 1] methods returned: {data['methods']}")

        # Must return a list of method names
        assert "methods" in data, "Response missing 'methods' key"
        assert isinstance(data["methods"], list), "'methods' must be a list"
        assert len(data["methods"]) > 0, "No methods extracted from connector.py"

        # BaseConnector internals must NOT appear (they break tests)
        forbidden = {
            "get_token",
            "set_token",
            "save_config",
            "get_config",
            "clear_token",
            "ingest_batch",
            "save_token",
        }
        leaked = forbidden & set(data["methods"])
        assert not leaked, (
            f"BaseConnector internals leaked into method list: {leaked}\n"
            "Fix: _extract_methods_from_source must scan only the connector class body."
        )

        # Store for next steps
        TestGenerateWorkflow._methods = data["methods"]

    # ── Step 2: generate-unit-tests via real Gemini (SSE) ────────────────────

    def test_step2_generate_unit_tests_via_gemini(self):
        """Step 2 — POST generate-unit-tests must stream SSE and end with generate_complete."""
        methods = getattr(TestGenerateWorkflow, "_methods", None)
        if not methods:
            pytest.skip("Step 1 did not produce methods — skipping Step 2")

        print(f"\n[Step 2] Sending {len(methods)} method(s) to Gemini: {methods}")
        t_start = time.time()

        raw_sse = ""
        with httpx.stream(
            "POST",
            f"{BASE_URL}/sessions/{SESSION_ID}/generate-unit-tests",
            headers={**HEADERS, "Accept": "text/event-stream"},
            json={"methods": methods},
            timeout=GENERATE_TIMEOUT,
            verify=False,  # noqa: S501 — test client against local dev server
        ) as resp:
            assert resp.status_code == 200, f"SSE endpoint returned {resp.status_code}: {resp.text}"
            for chunk in resp.iter_text():
                raw_sse += chunk
                # Print progress in real time so -s shows streaming
                for line in chunk.splitlines():
                    if line.startswith("data:"):
                        try:
                            ev = json.loads(line[5:].strip())
                            ev_type = ev.get("type", "")
                            msg = ev.get("message", "")
                            if ev_type in (
                                "generate_started",
                                "generate_progress",
                                "generate_complete",
                                "generate_error",
                            ):
                                print(f"  [{ev_type}] {msg[:120]}")
                        except Exception:
                            pass

        elapsed = time.time() - t_start
        print(f"\n[Step 2] SSE stream finished in {elapsed:.1f}s")

        events = _parse_sse_events(raw_sse)
        event_types = [e.get("type") for e in events]
        print(f"[Step 2] Event types seen: {event_types}")

        # Must not have errored
        error_events = [e for e in events if e.get("type") == "generate_error"]
        assert not error_events, f"generate_error received: {error_events[0].get('message')}"

        # Must end with generate_complete
        assert "generate_complete" in event_types, (
            f"Stream ended without generate_complete.\nEvents: {event_types}\nRaw tail: {raw_sse[-500:]}"
        )

        # generate_complete must carry code
        complete_ev = next(e for e in events if e.get("type") == "generate_complete")
        generated_code = complete_ev.get("generated_code", "")
        line_count = complete_ev.get("line_count", 0)
        print(f"[Step 2] Generated {line_count} lines of test code")

        assert len(generated_code) > 200, "Generated code is suspiciously short"
        assert "import pytest" in generated_code, "Generated code missing 'import pytest'"
        assert "def test_" in generated_code, "Generated code has no test functions"

        # Syntax auto-fix must have run (progress event logged it)
        progress_msgs = " ".join(e.get("message", "") for e in events if e.get("type") == "generate_progress")
        assert "auto-fix" in progress_msgs.lower() or "syntax" in progress_msgs.lower(), (
            "auto_fix_python_file step was not reported in progress events"
        )

        TestGenerateWorkflow._generated_code = generated_code

    # ── Step 2b: fix syntax errors via Gemini (mirrors "Attempt Fix" button) ──

    def test_step2b_fix_tests_if_syntax_error(self):
        """Step 2b — if generate_complete reported a syntax error, call fix/step/5 (write_tests).

        This mirrors what the UI 'Attempt Fix' button does.
        Skipped if Step 2 produced clean syntax.
        """
        generated_code = getattr(TestGenerateWorkflow, "_generated_code", None)
        if not generated_code:
            pytest.skip("Step 2 did not produce code")

        # Check if the generated file has a syntax error
        import ast as _ast

        try:
            _ast.parse(generated_code)
            print("\n[Step 2b] Generated code is syntax-clean — skipping fix step")
            TestGenerateWorkflow._fix_needed = False
            return
        except SyntaxError as exc:
            print(f"\n[Step 2b] Syntax error at line {exc.lineno}: {exc.msg} — calling fix/step/4")
            TestGenerateWorkflow._fix_needed = True

        # write_tests is step index 4 in this session's plan (Lambda: 5 steps, 0-4)
        WRITE_TESTS_STEP_INDEX = 4
        FIX_TIMEOUT = 180

        raw_sse = ""
        with httpx.stream(
            "POST",
            f"{BASE_URL}/sessions/{SESSION_ID}/fix/step/{WRITE_TESTS_STEP_INDEX}",
            headers={**HEADERS, "Accept": "text/event-stream"},
            json={},
            timeout=FIX_TIMEOUT,
            verify=False,  # noqa: S501 — test client against local dev server
        ) as resp:
            assert resp.status_code == 200, f"fix/step returned {resp.status_code}: {resp.text}"
            for chunk in resp.iter_text():
                raw_sse += chunk
                for line in chunk.splitlines():
                    if line.startswith("data:"):
                        try:
                            ev = json.loads(line[5:].strip())
                            ev_type = ev.get("type", "")
                            msg = ev.get("message", "")
                            if ev_type in ("fix_log", "fix_complete", "fix_error"):
                                print(f"  [{ev_type}] {str(msg)[:120]}")
                        except Exception:
                            pass

        events = _parse_sse_events(raw_sse)
        event_types = [e.get("type") for e in events]
        print(f"[Step 2b] Fix events: {event_types}")

        assert "fix_error" not in event_types, (
            f"fix_error received: {next(e.get('message') for e in events if e.get('type') == 'fix_error')}"
        )
        assert "fix_complete" in event_types, f"fix_complete not received. Events: {event_types}"

        # Re-read the fixed file
        test_file = (
            "/Users/vivekvarshavaishvik/Documents/Shielva Automation/shielva-connectors"
            "/generated_connectors/shielva-sense/gmail_connector/tests/test_connector.py"
        )
        from pathlib import Path as _Path

        fixed_code = _Path(test_file).read_text(encoding="utf-8")
        try:
            _ast.parse(fixed_code)
            print("[Step 2b] Fixed code is now syntax-clean ✅")
        except SyntaxError as exc:
            pytest.fail(f"Code still has syntax error after fix: line {exc.lineno}: {exc.msg}")

    # ── Step 3: run the generated tests ──────────────────────────────────────

    def test_step3_run_tests_on_generated_file(self):
        """Step 3 — POST /test must run pytest on the Gemini-generated file and pass."""
        from pathlib import Path as _Path

        _test_file = _Path(
            "/Users/vivekvarshavaishvik/Documents/Shielva Automation/shielva-connectors"
            "/generated_connectors/shielva-sense/gmail_connector/tests/test_connector.py"
        )
        if not getattr(TestGenerateWorkflow, "_generated_code", None):
            # Allow standalone run if file already exists on disk from previous run
            if _test_file.exists():
                TestGenerateWorkflow._generated_code = _test_file.read_text(encoding="utf-8")
            else:
                pytest.skip("No generated test file — run steps 1+2 first")

        print("\n[Step 3] Running pytest on generated test_connector.py ...")
        t_start = time.time()

        resp = httpx.post(
            f"{BASE_URL}/sessions/{SESSION_ID}/test",
            headers=HEADERS,
            timeout=RUN_TEST_TIMEOUT,
            verify=False,  # noqa: S501 — test client against local dev server
        )
        elapsed = time.time() - t_start
        print(f"[Step 3] Test run finished in {elapsed:.1f}s — HTTP {resp.status_code}")

        assert resp.status_code == 200, f"Run-tests endpoint returned {resp.status_code}: {resp.text}"

        result = resp.json()
        pytest_result = result.get("pytest", {})
        passed = pytest_result.get("passed", 0)
        failed = pytest_result.get("failed", 0)
        errors = pytest_result.get("errors", 0)
        overall = result.get("overall_pass", False)

        print(f"[Step 3] passed={passed}  failed={failed}  errors={errors}  overall_pass={overall}")

        # Print individual failures for easy debugging
        if failed or errors:
            for item in pytest_result.get("test_results", []):
                if item.get("outcome") != "passed":
                    print(f"  FAIL: {item.get('nodeid')} — {item.get('longrepr', '')[:300]}")

        # Store results for Step 3b to decide whether fix is needed
        TestGenerateWorkflow._step3_passed = passed
        TestGenerateWorkflow._step3_failed = failed
        TestGenerateWorkflow._step3_errors = errors
        TestGenerateWorkflow._step3_overall = overall

        if not overall:
            # Collect error details to feed into the fix step.
            # Prefer actual pytest output (ERRORS/FAILURES sections) over generic summaries
            # so the smart router and Gemini know the EXACT error to fix.
            failure_details = []
            raw_pytest_out = pytest_result.get("output", "")

            # Include ERRORS section (collection errors — e.g. class fixtures not supported)
            err_section_idx = raw_pytest_out.find("= ERRORS =")
            if err_section_idx >= 0:
                failure_details.append(raw_pytest_out[err_section_idx : err_section_idx + 3000])

            # Include individual test longrepr for failed/errored tests
            for item in pytest_result.get("test_results", []):
                if item.get("outcome") != "passed":
                    failure_details.append(f"{item.get('nodeid', '')}: {item.get('longrepr', '')[:400]}")

            # Fallback: include the raw pytest output tail if still empty
            if not failure_details and raw_pytest_out:
                failure_details.append(raw_pytest_out[-3000:])

            TestGenerateWorkflow._step3_failure_details = "\n\n".join(failure_details[:5])
            print("\n[Step 3] ⚠ Tests have failures — Step 3b will trigger fix/step/5")
        else:
            TestGenerateWorkflow._step3_failure_details = ""
            assert passed > 0, "No tests were collected/run"

    # ── Step 3b: fix failing tests via Gemini, then re-run ───────────────────

    def test_step3b_fix_and_rerun_if_failures(self):
        """Step 3b — if tests failed in Step 3, call fix/step/5 then re-run.

        Mirrors the UI 'Attempt Fix' button on the Run Tests step.
        Skipped if Step 3 already passed.
        """
        overall = getattr(TestGenerateWorkflow, "_step3_overall", None)
        if overall is True:
            print("\n[Step 3b] All tests passed in Step 3 — skipping fix")
            return
        if overall is None:
            pytest.skip("Step 3 did not run")

        passed = TestGenerateWorkflow._step3_passed
        failed = TestGenerateWorkflow._step3_failed
        errors = TestGenerateWorkflow._step3_errors
        error_details = getattr(TestGenerateWorkflow, "_step3_failure_details", "")

        print(f"\n[Step 3b] Step 3 had passed={passed} failed={failed} errors={errors}")

        # Fix the write_tests step (index 4 for Lambda — no separate run_tests step).
        # fix/step/4 uses smart routing based on root_cause + counts:
        #   errors > 0  → handle_fix_tests  (fix test file structure)
        #   test_failed > 0 → handle_fix_connector_for_tests  (TDD fix on connector)
        RUN_TESTS_STEP_INDEX = 4
        FIX_TIMEOUT = 300
        MAX_FIX_ATTEMPTS = 8  # Mirrors multiple "Attempt Fix" clicks in the UI

        # Use the actual pytest output we captured in step 3 as error_details.
        # This ensures Gemini and the smart router see the EXACT error message
        # (e.g. "class fixtures not supported") not a generic placeholder.
        if not error_details and (failed + errors) > 0:
            error_details = f"{failed} failed, {errors} collection error(s) — see server for details"

        overall2 = False
        p2 = f2 = e2 = 0

        for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
            print(f"\n[Step 3b] Fix attempt {attempt}/{MAX_FIX_ATTEMPTS} — calling fix/step/{RUN_TESTS_STEP_INDEX}...")
            raw_sse = ""
            with httpx.stream(
                "POST",
                f"{BASE_URL}/sessions/{SESSION_ID}/fix/step/{RUN_TESTS_STEP_INDEX}",
                headers={**HEADERS, "Accept": "text/event-stream"},
                json={"error_details": error_details},
                timeout=FIX_TIMEOUT,
                verify=False,  # noqa: S501 — test client against local dev server
            ) as resp:
                assert resp.status_code == 200, f"fix/step returned {resp.status_code}: {resp.text}"
                for chunk in resp.iter_text():
                    raw_sse += chunk
                    for line in chunk.splitlines():
                        if line.startswith("data:"):
                            try:
                                ev = json.loads(line[5:].strip())
                                ev_type = ev.get("type", "")
                                msg = ev.get("message", "")
                                if ev_type in ("fix_log", "fix_complete", "fix_error"):
                                    print(f"  [{ev_type}] {str(msg)[:120]}")
                            except Exception:
                                pass

            events = _parse_sse_events(raw_sse)
            event_types = [e.get("type") for e in events]
            print(
                f"[Step 3b] Attempt {attempt} fix events summary: fix_complete={('fix_complete' in event_types)} fix_error={('fix_error' in event_types)}"
            )

            assert "fix_error" not in event_types, (
                f"fix_error on attempt {attempt}: {next((e.get('message') for e in events if e.get('type') == 'fix_error'), '')}"
            )
            assert "fix_complete" in event_types, (
                f"fix_complete not received on attempt {attempt}. Events: {event_types}"
            )

            # Re-run tests after fix
            print(f"[Step 3b] Re-running tests after attempt {attempt}...")
            resp2 = httpx.post(
                f"{BASE_URL}/sessions/{SESSION_ID}/test",
                headers=HEADERS,
                timeout=300,
                verify=False,  # noqa: S501 — test client against local dev server
            )
            assert resp2.status_code == 200
            result2 = resp2.json()
            pytest2 = result2.get("pytest", {})
            p2, f2, e2 = (
                pytest2.get("passed", 0),
                pytest2.get("failed", 0),
                pytest2.get("errors", 0),
            )
            overall2 = result2.get("overall_pass", False)
            print(f"[Step 3b] Attempt {attempt} result: passed={p2}  failed={f2}  errors={e2}  overall_pass={overall2}")

            if overall2:
                print(f"[Step 3b] ✅ overall_pass=True after {attempt} fix attempt(s)")
                break

            if not overall2:
                # Collect fresh error details for next fix attempt
                fresh_details = []
                for item in pytest2.get("test_results", []):
                    if item.get("outcome") != "passed":
                        fresh_details.append(f"{item.get('nodeid', '')}: {item.get('longrepr', '')[:400]}")
                        print(f"  STILL FAIL: {item.get('nodeid')} — {item.get('longrepr', '')[:200]}")
                # Also extract ERRORS section from pytest output when test_results is empty
                # (happens when collection errors prevent pytest from even running tests)
                if not fresh_details:
                    raw_pytest_out = pytest2.get("output", "")
                    err_section_idx = raw_pytest_out.find("= ERRORS =")
                    if err_section_idx < 0:
                        err_section_idx = raw_pytest_out.find("ERRORS")
                    if err_section_idx >= 0:
                        fresh_details.append(raw_pytest_out[err_section_idx : err_section_idx + 2000])
                    elif raw_pytest_out:
                        fresh_details.append(raw_pytest_out[-2000:])
                error_details = "\n\n".join(fresh_details[:10]) if fresh_details else error_details

        # Note: pytest "errors" are setup/teardown errors in generated files —
        # the server's overall_pass is the authoritative pass/fail verdict.
        if e2 > 0 and overall2:
            print(f"  ⚠ {e2} setup error(s) in generated files (not failures) — overall_pass={overall2}")

        assert overall2, (
            f"Tests still failing after {MAX_FIX_ATTEMPTS} fix attempt(s).\npassed={p2}, failed={f2}, errors={e2}"
        )
        assert p2 > 0, "No tests passed after fix"
        assert f2 == 0, f"{f2} test(s) still failing after {MAX_FIX_ATTEMPTS} fix attempt(s)"
