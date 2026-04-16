#!/usr/bin/env python3
"""
Standalone test generation + auto-fix loop for shielva_gmail_connector.

Uses Gemini to:
  1. Generate pytest unit tests for all public connector methods
  2. Run pytest
  3. If any tests fail/error → ask Gemini to fix
  4. Loop forever until 100% green

Never gives up — every error is shown to Gemini with full traceback so it can reason
about exactly what went wrong and produce a correct fix.

Usage:
    python run_test_generation.py
"""

import ast
import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).resolve().parent
CONNECTOR_DIR = SCRIPT_DIR / "generated_connectors" / "shielva-sense" / "shielva_gmail_connector"
TESTS_DIR    = CONNECTOR_DIR / "tests"
TEST_FILE    = TESTS_DIR / "test_connector.py"
CONNECTOR_PY = CONNECTOR_DIR / "connector.py"

# Add required paths
sys.path.insert(0, str(CONNECTOR_DIR))
sys.path.insert(0, str(CONNECTOR_DIR.parent))
sys.path.insert(0, str(SCRIPT_DIR))

# ── Load .env (GEMINI_API_KEY etc.) ───────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(SCRIPT_DIR / "integration" / ".env")

GEMINI_API_KEY = os.environ.get("INTEGRATION_GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("INTEGRATION_GEMINI_MODEL", "gemini-2.0-flash")

if not GEMINI_API_KEY:
    print("ERROR: INTEGRATION_GEMINI_API_KEY not set in integration/.env")
    sys.exit(1)

# ── Gemini API call ────────────────────────────────────────────────────────────
import httpx, json

FALLBACK_MODEL = "gemini-2.0-flash"

async def call_gemini(system: str, user: str, max_tokens: int = 32768, _model: str = None) -> str:
    """Call Gemini streaming endpoint and return full response text.
    Retries on 503 with exponential backoff, then falls back to FALLBACK_MODEL.
    """
    model = _model or GEMINI_MODEL
    contents = [{"role": "user", "parts": [{"text": user}]}]
    payload = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    for attempt_num in range(6):  # up to 6 attempts (primary model × 3 + fallback × 3)
        use_model = model if attempt_num < 3 else FALLBACK_MODEL
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{use_model}:streamGenerateContent?alt=sse&key={GEMINI_API_KEY}"
        )
        if attempt_num == 3:
            print(f"\n  ⚠️  Switching to fallback model: {FALLBACK_MODEL}", flush=True)

        try:
            chunks = []
            chars = 0
            async with httpx.AsyncClient(timeout=300.0) as client:
                async with client.stream("POST", url, headers={"Content-Type": "application/json"}, json=payload) as resp:
                    if resp.status_code == 503:
                        body = await resp.aread()
                        wait = 2 ** attempt_num
                        print(f"\n  ⚠️  Gemini 503 (attempt {attempt_num+1}/6) — retrying in {wait}s...", flush=True)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status_code != 200:
                        body = await resp.aread()
                        raise RuntimeError(f"Gemini API error {resp.status_code}: {body.decode()[:300]}")
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            data = json.loads(data_str)
                            for cand in data.get("candidates", []):
                                for part in cand.get("content", {}).get("parts", []):
                                    txt = part.get("text", "")
                                    if txt:
                                        chunks.append(txt)
                                        chars += len(txt)
                                        if chars % 500 < len(txt):
                                            print(f"  ⚡ Gemini [{use_model}] generating... ({chars} chars)", end="\r", flush=True)
                        except Exception:
                            pass
            print()
            return "".join(chunks)
        except RuntimeError:
            raise
        except Exception as exc:
            if attempt_num >= 5:
                raise
            wait = 2 ** attempt_num
            print(f"\n  ⚠️  Error ({exc}) — retrying in {wait}s...", flush=True)
            await asyncio.sleep(wait)

    raise RuntimeError("Gemini API unavailable after 6 attempts")


# ── Pytest runner ──────────────────────────────────────────────────────────────
import sysconfig, site as _site

_SITE_PKGS  = sysconfig.get_paths().get("purelib", "")
_USER_SITE  = _site.getusersitepackages() if hasattr(_site, "getusersitepackages") else ""

def run_pytest() -> dict:
    """Run pytest on TESTS_DIR and return {returncode, output, passed, failed, errors}."""
    pythonpath = os.pathsep.join(filter(None, [
        str(CONNECTOR_DIR), str(CONNECTOR_DIR.parent),
        _SITE_PKGS, _USER_SITE, str(SCRIPT_DIR),
    ]))

    # Ensure pytest.ini exists
    pytest_ini = CONNECTOR_DIR / "pytest.ini"
    if not pytest_ini.exists():
        pytest_ini.write_text("[pytest]\nasyncio_mode = auto\n", encoding="utf-8")

    # Ensure conftest with asyncio_mode
    conftest = TESTS_DIR / "conftest.py"
    if not conftest.exists():
        conftest.write_text(
            "import pytest\n\ndef pytest_configure(config):\n"
            "    config.addinivalue_line('markers', 'asyncio: mark test as async')\n",
            encoding="utf-8",
        )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(TESTS_DIR),
         "-v", "--tb=long", "--no-header", f"--rootdir={CONNECTOR_DIR}"],
        capture_output=True, text=True, timeout=120,
        cwd=str(CONNECTOR_DIR),
        env={**os.environ, "PYTHONPATH": pythonpath},
    )
    output = result.stdout + result.stderr
    passed = output.count(" PASSED")
    failed = output.count(" FAILED")
    errors = output.count("ERROR collecting") + output.count("ERROR tests/")
    return {"returncode": result.returncode, "output": output, "passed": passed, "failed": failed, "errors": errors}


# ── AST helpers ────────────────────────────────────────────────────────────────
def extract_public_methods(source: str) -> list[str]:
    """Return public method names from the BaseConnector subclass."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = [b.id if isinstance(b, ast.Name) else (b.attr if isinstance(b, ast.Attribute) else "") for b in node.bases]
            if "BaseConnector" in bases:
                return [
                    n.name for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not n.name.startswith("_")
                ]
    return []


def strip_markdown_fences(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        lines = code.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        code = "\n".join(lines)
    return code.strip()


def get_real_names_from_connector(connector_path: Path) -> set:
    """Collect all exported names from connector.py and sibling .py files."""
    real: set = set()
    def _collect(path: Path):
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            for n in ast.walk(tree):
                if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    real.add(n.name)
                elif isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            real.add(t.id)
                elif isinstance(n, ast.ImportFrom) and n.names:
                    for alias in n.names:
                        real.add(alias.asname or alias.name)
        except Exception:
            pass
    _collect(connector_path)
    for f in connector_path.parent.glob("*.py"):
        if f != connector_path:
            _collect(f)
    return real


def strip_hallucinated_imports(code: str, real_names: set) -> str:
    """Remove names not in real_names from 'from connector import X, Y' lines."""
    lines = code.splitlines()
    out = []
    for line in lines:
        m = re.match(r'^(from connector import )(.+)$', line.strip())
        if m:
            prefix = m.group(1)
            names = [n.strip() for n in m.group(2).split(",")]
            valid = [n for n in names if n in real_names or not n]
            if not valid:
                continue
            indent = len(line) - len(line.lstrip())
            out.append(" " * indent + prefix + ", ".join(valid))
        else:
            out.append(line)
    return "\n".join(out)


# ── System prompt for generation ───────────────────────────────────────────────
def build_gen_system_prompt(class_name: str, all_sigs: str, private_sigs: str) -> str:
    return (
        "You are a Python testing expert. Output ONLY Python code — no prose, no markdown fences.\n"
        "The very first character of your response must be the first character of the Python file.\n\n"
        "## ⚠️ MANDATORY IMPORTS — copy these exactly:\n"
        "import pytest\n"
        "import httpx\n"
        "from unittest.mock import patch, MagicMock, AsyncMock\n"
        "from googleapiclient.errors import HttpError\n"
        f"from connector import {class_name}\n"
        "from shared.base_connector import (\n"
        "    BaseConnector, ConnectorStatus, ConnectorHealth, AuthStatus,\n"
        "    TokenInfo, NormalizedDocument, SyncResult, SyncStatus\n"
        ")\n\n"
        "## ⚠️ CRITICAL RULES:\n"
        "## 1. EVERY mock parameter must have a matching @patch.object decorator above it.\n"
        "##    WRONG:  async def test_x(self, mock_save, connector)  ← no decorator\n"
        f"##    RIGHT:  @patch.object({class_name}, 'save_config', new_callable=AsyncMock)\n"
        "##             async def test_x(self, mock_save, connector)  ← decorator present\n"
        f"## NOTE: save_config IS a real async method on {class_name} — patch it without create=True\n"
        "## 2. Use @pytest.fixture at MODULE LEVEL (not inside a class) for shared fixtures.\n"
        "## 3. ALWAYS mock BaseConnector async methods: set_token, get_token, ingest_batch\n"
        "##    They connect to a real database — NEVER let them run in tests.\n"
        "##    Use @patch.object on every test that calls install/authorize/sync.\n"
        "## 4. Use AsyncMock for async methods, MagicMock for sync methods.\n"
        "## 5. For HttpError: use MagicMock resp with .status and .reason — NEVER httpx.Response\n"
        "## 6. NEVER import: freezegun, factory_boy, hypothesis, faker\n"
        "## 7. @pytest.mark.asyncio is OPTIONAL — asyncio_mode=auto is set in pytest.ini\n\n"
        f"## REAL PUBLIC METHODS on {class_name} (test ONLY these):\n{all_sigs}\n\n"
        f"## REAL PRIVATE METHODS (patch with patch.object if needed):\n{private_sigs}\n\n"
        f"## CONSTRUCTOR: {class_name}(tenant_id='test-tenant', connector_id='test-connector')\n"
        "## NEVER pass token_info, credentials, or config to __init__.\n\n"
        "## ConnectorStatus fields: connector_id, health(ConnectorHealth), auth_status(AuthStatus), message(str)\n"
        "## ConnectorHealth: HEALTHY, DEGRADED, OFFLINE, UNHEALTHY\n"
        "## AuthStatus: PENDING, CONNECTED, EXPIRED, FAILED, MISSING_CREDENTIALS, TOKEN_EXPIRED, AUTHENTICATED\n"
        "## SyncStatus: IDLE, SYNCING, COMPLETED, FAILED, SUCCESS, PARTIAL\n"
    )


def build_fix_system_prompt(class_name: str) -> str:
    return (
        "You are a Python testing expert fixing failing pytest tests. Output ONLY Python code.\n"
        "The very first character of your response must be the first character of the Python file.\n\n"
        "## YOUR JOB:\n"
        "Fix ALL errors in the test file so every test passes. Read the pytest output carefully.\n"
        "Do NOT weaken assertions — fix the mock setup instead.\n\n"
        "## MANDATORY RULES — violations cause collection errors:\n"
        "## 1. EVERY mock parameter needs a matching @patch.object decorator DIRECTLY above the function.\n"
        f"##    Use: @patch.object({class_name}, 'method_name', new_callable=AsyncMock)\n"
        "##    NEVER add a mock parameter without its decorator.\n"
        "## 2. @pytest.fixture MUST be at MODULE LEVEL — never inside a class body.\n"
        "## 3. Fixtures shared across test classes go in conftest.py or at module top.\n"
        "## 4. Always mock: set_token, get_token, clear_token, ingest_batch (they hit DB/network).\n"
        "## 5. AsyncMock for any method that is `async def`. MagicMock for sync.\n"
        "## 6. HttpError mock: use MagicMock with .status and .reason, NEVER httpx.Response.\n"
        "## 7. For assertion failures: read the actual error and adjust mock return values.\n"
        "## 8. Re-output the COMPLETE fixed test file — do not truncate.\n"
    )


# ── Main loop ─────────────────────────────────────────────────────────────────
async def main():
    print("=" * 70)
    print(f"  Shielva Gmail Connector — Gemini Test Generation + Auto-Fix Loop")
    print(f"  Model: {GEMINI_MODEL}")
    print("=" * 70)

    if not CONNECTOR_PY.exists():
        print(f"ERROR: connector.py not found at {CONNECTOR_PY}")
        sys.exit(1)

    connector_source = CONNECTOR_PY.read_text(encoding="utf-8")

    # Collect all connector source for context
    source_context = ""
    for py_file in sorted(CONNECTOR_DIR.rglob("*.py")):
        if "__pycache__" in str(py_file) or "tests" in py_file.parts:
            continue
        try:
            rel = py_file.relative_to(CONNECTOR_DIR)
            source_context += f"\n# === {rel} ===\n{py_file.read_text(encoding='utf-8')}\n"
        except Exception:
            pass

    # Extract class name and method signatures
    cls_match = re.search(r'^class\s+(\w+)\s*\(BaseConnector\)', connector_source, re.MULTILINE)
    class_name = cls_match.group(1) if cls_match else "Connector"

    all_sigs  = re.findall(r'^\s+(async def \w+\s*\([^)]*\)[^:]*:)', connector_source, re.MULTILINE)
    pub_sigs  = "\n".join("  " + s.strip() for s in all_sigs if not re.search(r"def _", s))
    priv_sigs = "\n".join("  " + s.strip() for s in all_sigs if re.search(r"def _", s))

    public_methods = extract_public_methods(connector_source)
    real_names = get_real_names_from_connector(CONNECTOR_PY)

    print(f"\n📋 Found {len(public_methods)} public methods: {', '.join(public_methods)}")
    print(f"🤖 Using Gemini model: {GEMINI_MODEL}\n")

    TESTS_DIR.mkdir(exist_ok=True)

    # ── PHASE 1: Generate initial tests ───────────────────────────────────────
    print("─" * 70)
    print("PHASE 1: Generating tests via Gemini...")
    print("─" * 70)

    methods_list = "\n".join(f"- {m}" for m in public_methods)
    system_prompt = build_gen_system_prompt(class_name, pub_sigs, priv_sigs)
    user_prompt = (
        f"Generate pytest unit tests for ALL these connector methods:\n{methods_list}\n\n"
        f"Complete connector source:\n{source_context}\n\n"
        "Requirements:\n"
        "- One test class per method (e.g. TestInstall, TestAuthorize)\n"
        "- At least 2 test cases per method: happy path + error case\n"
        "- Mock ALL database and HTTP calls\n"
        "- Every mock parameter MUST have a @patch.object decorator above it\n"
        "- Never call a real network or database in any test\n"
    )

    generated_code = await call_gemini(system_prompt, user_prompt)
    generated_code = strip_markdown_fences(generated_code)

    # Strip hallucinated imports
    generated_code = strip_hallucinated_imports(generated_code, real_names)

    TEST_FILE.write_text(generated_code, encoding="utf-8")
    line_count = generated_code.count("\n") + 1
    print(f"✅ Generated {line_count} lines → written to {TEST_FILE.relative_to(SCRIPT_DIR)}\n")

    # ── PHASE 2: Run → Fix loop ────────────────────────────────────────────────
    attempt = 0
    while True:
        attempt += 1
        print("─" * 70)
        print(f"RUN #{attempt}: Running pytest...")
        print("─" * 70)

        t0 = time.time()
        pytest_result = run_pytest()
        elapsed = time.time() - t0

        passed = pytest_result["passed"]
        failed = pytest_result["failed"]
        errors = pytest_result["errors"]
        output = pytest_result["output"]

        print(f"\n  Result: {passed} passed, {failed} failed, {errors} collection errors  ({elapsed:.1f}s)\n")

        # Print relevant output lines
        for line in output.splitlines():
            stripped = line.strip()
            if any(k in stripped for k in ["PASSED", "FAILED", "ERROR", "ERRORS", "passed", "failed", "error"]):
                print(f"  {stripped}")

        if pytest_result["returncode"] == 0:
            print("\n" + "=" * 70)
            print(f"  🎉 ALL TESTS PASSED on attempt #{attempt}!")
            print("=" * 70)
            break

        if passed == 0 and failed == 0 and errors == 0:
            print("  ⚠️  No tests ran — possibly syntax error in test file or empty output.")

        print(f"\n─── FIX #{attempt}: Sending full error to Gemini ───")

        # Build comprehensive fix prompt with full pytest output
        current_test_code = TEST_FILE.read_text(encoding="utf-8")
        fix_system = build_fix_system_prompt(class_name)
        fix_user = (
            f"## Fix attempt #{attempt}\n\n"
            f"## Full pytest output (read every line carefully):\n"
            f"```\n{output[:12000]}\n```\n\n"
            f"## Current test file:\n"
            f"```python\n{current_test_code}\n```\n\n"
            f"## Connector source (for correct method signatures and return types):\n"
            f"```python\n{connector_source}\n```\n\n"
            f"## Fix instructions:\n"
            f"- {passed} passed, {failed} failed, {errors} collection errors\n"
            f"- Collection errors (ERROR) mean the test class/function couldn't be collected.\n"
            f"  Most common cause: mock parameter without a @patch.object decorator above the test method.\n"
            f"- Test failures (FAILED) mean the test ran but an assertion or exception was wrong.\n"
            f"  Read the actual vs expected values and fix mock return_value or assertions.\n"
            f"- Output the COMPLETE corrected test file. Do NOT truncate.\n"
        )

        fixed_code = await call_gemini(fix_system, fix_user, max_tokens=32768)
        fixed_code = strip_markdown_fences(fixed_code)
        fixed_code = strip_hallucinated_imports(fixed_code, real_names)

        # Validate syntax before writing
        try:
            ast.parse(fixed_code)
        except SyntaxError as e:
            print(f"  ⚠️ Gemini produced a syntax error ({e}) — asking for a quick syntax fix...")
            syntax_fix = await call_gemini(
                "You are fixing a Python syntax error. Output ONLY the corrected Python code.",
                f"Fix this SyntaxError on line {e.lineno}: {e.msg}\n\n```python\n{fixed_code}\n```"
            )
            syntax_fix = strip_markdown_fences(syntax_fix)
            try:
                ast.parse(syntax_fix)
                fixed_code = syntax_fix
            except SyntaxError:
                print("  ⚠️  Syntax fix also failed — keeping previous version and retrying generation")
                # Regenerate from scratch
                fixed_code = await call_gemini(system_prompt, user_prompt)
                fixed_code = strip_markdown_fences(fixed_code)
                fixed_code = strip_hallucinated_imports(fixed_code, real_names)

        TEST_FILE.write_text(fixed_code, encoding="utf-8")
        fix_lines = fixed_code.count("\n") + 1
        print(f"  ✅ Fix written ({fix_lines} lines) — running tests again...\n")


if __name__ == "__main__":
    asyncio.run(main())
