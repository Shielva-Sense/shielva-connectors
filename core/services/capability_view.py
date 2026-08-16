"""What a connector can DO, projected for the catalog listing.

Its own module rather than a helper inside gateway.py, for one practical reason:
gateway.py imports FastAPI, redis, motor and the rest, so a test that reaches
this function through it needs the whole service installed. CI's coverage stage
installs only pytest and its plugins, so that import fails, the test is never
collected, and Sonar reports 0% coverage on new code — which looks like missing
tests rather than a missing dependency.

A pure function with no imports at all is testable anywhere, which is what makes
the coverage number real.
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
