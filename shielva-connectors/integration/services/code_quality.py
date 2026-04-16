"""Integration Builder — Code quality analysis."""

import ast
import re
import subprocess
import structlog
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = structlog.get_logger(__name__)


# ── Non-AI syntax auto-fixer ─────────────────────────────────────────────────

def auto_fix_python_file(path: Path) -> Dict[str, Any]:
    """Run a chain of non-AI rule-based fixers on a Python file, then verify with ast.parse.

    NOTE: test files (test_*.py) are intentionally skipped — autoflake/ruff can remove
    imports that are only used inside pytest.raises() or assertion helpers, breaking tests.

    Pipeline (in order):
      1. autoflake  — removes unused imports and unused variables
      2. ruff       — fixes hundreds of style/lint issues (PEP 8, import order, etc.)
      3. ast.parse  — final validation: is the file syntactically clean?

    Returns a dict::
        {
            "clean": bool,          # True if ast.parse passes after all fixes
            "tools_applied": list,  # which tools ran without error
            "tools_failed": list,   # which tools returned non-zero exit
            "syntax_error": str,    # present only when clean=False
        }

    This is intentionally non-AI — it runs deterministically in milliseconds and
    resolves the majority of generation artefacts (unused imports, mixed indentation,
    trailing commas, missing newlines) before falling back to Gemini.
    """
    # Skip test files — autoflake removes imports used only inside pytest.raises/assertions
    if path.name == "conftest.py" or path.name.startswith("test_") or path.name.endswith("_test.py"):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            # ── Pre-fix: insert `pass` into empty function/class bodies (IndentationError) ──
            # Gemini sometimes generates `async def test_foo(self):\n@decorator` with no body.
            # Detect and insert `pass` so ast.parse can proceed and other fixes can run.
            if "expected an indented block" in (exc.msg or "").lower() or "indentation" in (exc.msg or "").lower():
                try:
                    _fixed_lines = src.splitlines()
                    _inserted = False
                    _i = 0
                    while _i < len(_fixed_lines):
                        _line = _fixed_lines[_i]
                        # Detect `def foo(...):` or `async def foo(...):` or `class Foo:` with no body
                        if re.match(r'^(\s*)(async\s+)?def\s+\w+.*:\s*$', _line) or re.match(r'^(\s*)class\s+\w+.*:\s*$', _line):
                            _indent_m = re.match(r'^(\s*)', _line)
                            _base_indent = _indent_m.group(1) if _indent_m else ""
                            _body_indent = _base_indent + "    "
                            # Check if next line exists and is NOT indented more (empty body)
                            _next_i = _i + 1
                            while _next_i < len(_fixed_lines) and not _fixed_lines[_next_i].strip():
                                _next_i += 1
                            if _next_i >= len(_fixed_lines) or not _fixed_lines[_next_i].startswith(_body_indent):
                                # Completely empty body
                                _fixed_lines.insert(_i + 1, f"{_body_indent}pass")
                                _inserted = True
                            else:
                                # Body exists — check if it's comment-only (no executable statements)
                                _body_scan = _next_i
                                _has_executable = False
                                while _body_scan < len(_fixed_lines):
                                    _bl = _fixed_lines[_body_scan]
                                    if not _bl.strip():
                                        _body_scan += 1
                                        continue
                                    if not _bl.startswith(_body_indent):
                                        break
                                    if not _bl.strip().startswith("#"):
                                        _has_executable = True
                                        break
                                    _body_scan += 1
                                if not _has_executable:
                                    # Comment-only body — insert pass as first statement
                                    _fixed_lines.insert(_i + 1, f"{_body_indent}pass")
                                    _inserted = True
                        _i += 1
                    if _inserted:
                        _fixed_src = "\n".join(_fixed_lines)
                        ast.parse(_fixed_src)  # validate fix
                        src = _fixed_src
                        path.write_text(src, encoding="utf-8")
                        tree = ast.parse(src)
                        # Continue to class fixture checks below
                except Exception:
                    return {"clean": False, "tools_applied": [], "tools_failed": [], "syntax_error": f"line {exc.lineno}: {exc.msg}"}
            else:
                return {"clean": False, "tools_applied": [], "tools_failed": [], "syntax_error": f"line {exc.lineno}: {exc.msg}"}

        # ── Auto-fix: remove @pytest.fixture applied to a class (test class, not fixture fn) ──
        # Also auto-fix: move @pytest.fixture from inside a class body to module level
        # Both cause ValueError("class fixtures not supported") at collection time.
        _lines = src.splitlines()
        _changed = False

        # (0) For conftest.py: delete module-level fixtures that have `self` as first param.
        # Gemini writes `def setup_mocks(self, connector):` at module level — the body uses
        # self.x = ... throughout, so just removing `self` from params leaves NameErrors.
        # Delete the whole fixture; the fix loop will then tell Gemini to rewrite properly.
        if path.name == "conftest.py":
            _conf_del_lines: set = set()
            for _cn in ast.iter_child_nodes(tree):
                if not isinstance(_cn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                _has_fix = any(
                    (isinstance(_d, ast.Attribute) and _d.attr == "fixture") or
                    (isinstance(_d, ast.Call) and isinstance(_d.func, ast.Attribute) and _d.func.attr == "fixture") or
                    (isinstance(_d, ast.Name) and _d.id == "fixture")
                    for _d in _cn.decorator_list
                )
                if not _has_fix:
                    continue
                _fn_args = _cn.args.args
                _has_self_param = _fn_args and _fn_args[0].arg == "self"
                # Also catch: self already removed from params but body still uses self.x = ...
                _has_self_body = any(
                    isinstance(_sn, ast.Attribute) and
                    isinstance(_sn.value, ast.Name) and _sn.value.id == "self"
                    for _sn in ast.walk(_cn)
                )
                if _has_self_param or _has_self_body:
                    _dstart = min((d.lineno for d in _cn.decorator_list), default=_cn.lineno) - 1
                    _dend = getattr(_cn, "end_lineno", _cn.lineno)
                    _conf_del_lines.update(range(_dstart, _dend))
            if _conf_del_lines:
                _lines = [l for i, l in enumerate(_lines) if i not in _conf_del_lines]
                src = "\n".join(_lines)
                _changed = True

        # (A) Remove @pytest.fixture decorators applied to ClassDef nodes
        _class_deco_lines: set = set()
        for _top in ast.iter_child_nodes(tree):
            if isinstance(_top, ast.ClassDef):
                for _dec in _top.decorator_list:
                    if (
                        (isinstance(_dec, ast.Attribute) and _dec.attr == "fixture") or
                        (isinstance(_dec, ast.Name) and _dec.id == "fixture") or
                        (isinstance(_dec, ast.Call) and (
                            (isinstance(_dec.func, ast.Attribute) and _dec.func.attr == "fixture") or
                            (isinstance(_dec.func, ast.Name) and _dec.func.id == "fixture")
                        ))
                    ):
                        _class_deco_lines.add(_dec.lineno - 1)  # 0-indexed

        if _class_deco_lines:
            _lines = [l for i, l in enumerate(_lines) if i not in _class_deco_lines]
            src = "\n".join(_lines)
            _changed = True

        # (B) Move @pytest.fixture from inside a class body to module level
        try:
            _tree2 = ast.parse(src)
            _lines2 = src.splitlines()
            _extractions2: list = []
            _existing_mod_fixtures: set = set()

            # Collect module-level fixture names
            for _top in ast.iter_child_nodes(_tree2):
                if isinstance(_top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for _d in _top.decorator_list:
                        if (
                            (isinstance(_d, ast.Attribute) and _d.attr == "fixture") or
                            (isinstance(_d, ast.Name) and _d.id == "fixture") or
                            (isinstance(_d, ast.Call) and (
                                (isinstance(_d.func, ast.Attribute) and _d.func.attr == "fixture") or
                                (isinstance(_d.func, ast.Name) and _d.func.id == "fixture")
                            ))
                        ):
                            _existing_mod_fixtures.add(_top.name)

            for _cls in ast.walk(_tree2):
                if not isinstance(_cls, ast.ClassDef):
                    continue
                for _fn in list(_cls.body):
                    if not isinstance(_fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for _d in _fn.decorator_list:
                        if (
                            (isinstance(_d, ast.Attribute) and _d.attr == "fixture") or
                            (isinstance(_d, ast.Name) and _d.id == "fixture") or
                            (isinstance(_d, ast.Call) and (
                                (isinstance(_d.func, ast.Attribute) and _d.func.attr == "fixture") or
                                (isinstance(_d.func, ast.Name) and _d.func.id == "fixture")
                            ))
                        ):
                            _dec_start = _d.lineno - 1
                            _fn_end = getattr(_fn, "end_lineno", _fn.lineno)
                            _extractions2.append({
                                "class_lineno_0": _cls.lineno - 1,
                                "fn_start_0": _dec_start,
                                "fn_end_0": _fn_end,
                                "fn_name": _fn.name,
                            })
                            break

            if _extractions2:
                _new_lines2 = list(_lines2)
                for _ext in sorted(_extractions2, key=lambda x: x["fn_start_0"], reverse=True):
                    _raw = _new_lines2[_ext["fn_start_0"]:_ext["fn_end_0"]]
                    _dedented = [l[4:] if l.startswith("    ") else l for l in _raw]
                    del _new_lines2[_ext["fn_start_0"]:_ext["fn_end_0"]]
                    if _ext["fn_name"] not in _existing_mod_fixtures:
                        _ins = min(_ext["class_lineno_0"], len(_new_lines2))
                        _new_lines2[_ins:_ins] = _dedented + [""]
                        _existing_mod_fixtures.add(_ext["fn_name"])
                src = "\n".join(_new_lines2)
                _changed = True
        except Exception:
            pass  # AST parse failed on mid-fix code — skip step B

        if _changed:
            try:
                ast.parse(src)
                path.write_text(src, encoding="utf-8")
                return {"clean": True, "tools_applied": ["class-fixture-autofix"], "tools_failed": []}
            except SyntaxError as exc:
                # Revert — auto-fix broke something
                path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

        try:
            ast.parse(path.read_text(encoding="utf-8"))
            return {"clean": True, "tools_applied": [], "tools_failed": [], "skipped": "test file"}
        except SyntaxError as exc:
            return {"clean": False, "tools_applied": [], "tools_failed": [], "syntax_error": f"line {exc.lineno}: {exc.msg}"}

    result: Dict[str, Any] = {"clean": False, "tools_applied": [], "tools_failed": []}
    file_str = str(path)

    # 1. autoflake — remove unused imports / variables
    try:
        proc = subprocess.run(
            [
                "autoflake",
                "--in-place",
                "--remove-all-unused-imports",
                "--remove-unused-variables",
                file_str,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            result["tools_applied"].append("autoflake")
        else:
            result["tools_failed"].append(f"autoflake (exit {proc.returncode}): {proc.stderr[:200]}")
    except FileNotFoundError:
        result["tools_failed"].append("autoflake (not installed)")
    except Exception as exc:
        result["tools_failed"].append(f"autoflake ({exc})")

    # 2. ruff --fix — PEP 8, import sorting, and hundreds of lint auto-fixes
    try:
        proc = subprocess.run(
            [
                "ruff",
                "check",
                "--fix",
                "--unsafe-fixes",
                "--select", "E,F,W,I",   # errors, pyflakes, warnings, isort
                file_str,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # ruff exits 0 (all clean) or 1 (issues found/fixed) — both are fine
        result["tools_applied"].append("ruff")
    except FileNotFoundError:
        result["tools_failed"].append("ruff (not installed)")
    except Exception as exc:
        result["tools_failed"].append(f"ruff ({exc})")

    # 3. ast.parse — final syntax check
    try:
        content = path.read_text(encoding="utf-8")
        ast.parse(content)
        result["clean"] = True
    except SyntaxError as exc:
        result["clean"] = False
        result["syntax_error"] = f"line {exc.lineno}: {exc.msg}"
    except Exception as exc:
        result["clean"] = False
        result["syntax_error"] = str(exc)

    logger.info(
        "code_quality.auto_fix",
        path=path.name,
        clean=result["clean"],
        applied=result["tools_applied"],
        failed=result["tools_failed"],
        syntax_error=result.get("syntax_error"),
    )
    return result


def analyze_file(path: str) -> Dict[str, Any]:
    """Analyze a Python file and return quality metrics.

    Returns:
        Dict with: line_count, function_count, class_count, docstring_coverage,
                   has_type_hints, import_count, quality_score (0-100)
    """
    try:
        content = Path(path).read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("code_quality.read_failed", path=path, error=str(exc))
        return {"quality_score": 0, "error": str(exc)}

    lines = content.split("\n")
    line_count = len(lines)
    blank_lines = sum(1 for l in lines if not l.strip())
    comment_lines = sum(1 for l in lines if l.strip().startswith("#"))

    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError as exc:
        return {
            "line_count": line_count,
            "quality_score": 0,
            "syntax_error": str(exc),
        }

    # Count constructs
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]

    # Docstring coverage
    documented = 0
    total_docable = len(functions) + len(classes)
    for node in functions + classes:
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        ):
            documented += 1

    docstring_coverage = (documented / total_docable * 100) if total_docable > 0 else 100

    # Type hint presence (check function annotations)
    typed_functions = 0
    for fn in functions:
        if fn.returns is not None or any(arg.annotation for arg in fn.args.args):
            typed_functions += 1
    type_hint_coverage = (typed_functions / len(functions) * 100) if functions else 100

    # Quality score (weighted)
    score = 0.0
    score += min(docstring_coverage, 100) * 0.30  # 30% docstrings
    score += min(type_hint_coverage, 100) * 0.20   # 20% type hints
    score += (20 if line_count > 10 else 5)         # 20% substantive code
    score += (15 if len(classes) >= 1 else 5)        # 15% has class structure
    score += (15 if len(functions) >= 3 else 5)      # 15% enough functions

    return {
        "line_count": line_count,
        "blank_lines": blank_lines,
        "comment_lines": comment_lines,
        "function_count": len(functions),
        "class_count": len(classes),
        "import_count": len(imports),
        "docstring_coverage": round(docstring_coverage, 1),
        "type_hint_coverage": round(type_hint_coverage, 1),
        "has_type_hints": type_hint_coverage > 50,
        "quality_score": round(min(score, 100), 1),
    }


def analyze_directory(directory: str) -> Dict[str, Any]:
    """Analyze all Python files in a directory."""
    root = Path(directory)
    if not root.exists():
        return {"error": "Directory not found", "files": []}

    py_files = list(root.rglob("*.py"))
    results = []
    total_score = 0.0

    for f in py_files:
        metrics = analyze_file(str(f))
        metrics["path"] = str(f.relative_to(root))
        results.append(metrics)
        total_score += metrics.get("quality_score", 0)

    avg_score = (total_score / len(results)) if results else 0

    return {
        "file_count": len(results),
        "average_quality_score": round(avg_score, 1),
        "total_lines": sum(r.get("line_count", 0) for r in results),
        "files": results,
    }
