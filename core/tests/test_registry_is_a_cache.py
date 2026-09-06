"""Connected is a fact about the stored token, not about this process's memory.

🚨 Publishing 31 connector versions turned every card in the console back to
"Ready To Connect" while every token was still valid in Mongo. Two independent
causes, both the same mistake — treating the in-memory registry as the truth:

  * /connectors/list read `connector_id in registry._connectors` for the badge.
  * _resolve_for_tenant looked only in memory, so a miss became
    "Connector not found" rather than a rebuild.

Anything that empties the registry hits both: a pod restart, a rescheduled
replica, or a wheel upgrade evicting instances of the class it replaced. The
only connector that kept its badge was the one whose wheel had not changed.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

_SRC = (_CORE / "gateway.py").read_text()
_TREE = ast.parse(_SRC)


def _fn(name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    return next(n for n in ast.walk(_TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _body(name: str) -> str:
    fn = _fn(name)
    return "\n".join(_SRC.splitlines()[fn.lineno - 1 : fn.end_lineno])


def test_the_listing_badge_comes_from_the_token_store() -> None:
    body = _body("list_tenant_connectors")
    assert "get_connector_tokens" in body, "the badge must ask the store, not the registry"
    # Code only — the comment above the fix quotes the line it replaced.
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("#"))
    assert "registry._connectors" not in code, "in-memory membership is back in the badge"


def test_a_registry_miss_rebuilds_from_the_store() -> None:
    body = _body("_resolve_for_tenant")
    assert "_rehydrate_for_tenant" in body


def test_the_rebuild_is_actually_reachable() -> None:
    """It was first written after a `return None` — present, reviewed, and dead."""
    fn = _fn("_resolve_for_tenant")
    last = fn.body[-1]
    assert isinstance(last, ast.Return)
    assert "_rehydrate_for_tenant" in ast.dump(last), (
        "the rebuild must be the function's fallthrough, not stranded after a return"
    )


def test_every_resolve_call_is_awaited() -> None:
    """The resolver became a coroutine; a missed `await` returns a coroutine
    object that is truthy, so the caller sails past its own `if not connector`
    guard and fails later with something unrelated."""
    missed = [
        (i + 1, ln.strip())
        for i, ln in enumerate(_SRC.splitlines())
        if "_resolve_for_tenant(connector_id" in ln and "await _resolve_for_tenant" not in ln and "async def" not in ln
    ]
    assert missed == [], f"un-awaited resolver calls: {missed}"


def test_rehydration_reuses_the_startup_steps() -> None:
    """install() sets up the auth handler, initialize() loads the stored token,
    and install()'s own result is kept — a restore that skips the last one leaves
    every token-authenticated connector reporting `pending` forever."""
    body = _body("_rehydrate_connector")
    for step in ("install()", "initialize()", "_status", "registry.register("):
        assert step in body, f"rehydration is missing {step}"
