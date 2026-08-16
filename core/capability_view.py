"""What a connector can DO, projected for the catalog listing.

At the TOP of core/, not inside services/, and not a helper in gateway.py —
both of those were tried and both broke the same way.

CI's coverage stage installs pytest and its plugins, nothing else. gateway.py
imports FastAPI, redis and motor, so reaching this through it fails there. And
services/__init__.py imports credentials + encryption, so even
`from services.capability_view import …` drags in cryptography and fails too.
Either way the test is never collected and Sonar reports 0% coverage on new
code, which reads as "untested" when the cause is a missing dependency.

Here it imports nothing and belongs to no package, so it is importable with a
bare interpreter — which is what makes the coverage number real.
"""

from __future__ import annotations

from typing import Any


def capability_view(apis: list | None) -> dict[str, Any]:
    """Project a catalog doc's `apis` into what the designer needs.

    Two fields, because the SMS / Mail / Calendar / CRM nodes ask two different
    questions: `capabilities` answers "which connectors can send an SMS" for the
    picker, and `capability_actions` carries the param schema and field map so
    the chosen one renders its form and stays swappable — without a request per
    connector, which a picker over 200+ of them cannot afford.

    Only actions that DECLARE a capability are carried. Projecting the whole
    `apis` array would multiply a 263KB response by the ~20 read actions each
    connector has, for data no caller wants.
    """
    actions = [a for a in (apis or []) if isinstance(a, dict) and a.get("capability")]
    return {
        "capabilities": sorted({a["capability"] for a in actions}),
        "capability_actions": [
            {
                "capability": a["capability"],
                "action": a.get("id"),
                "label": a.get("name") or a.get("id"),
                "method": a.get("method"),
                "map": a.get("capability_map"),
                "opaque": bool(a.get("capability_opaque")),
                "params": a.get("params") or [],
            }
            for a in actions
        ],
    }
