"""What `/connectors/types` tells the designer a connector can DO.

The SMS / Mail / Calendar / CRM nodes are built entirely on this projection: it
decides which connectors appear in each picker, and what the chosen one's form
looks like. Getting it wrong is not a visual bug — a dropped `map` silently
makes a provider un-swappable, which is the whole reason the field exists.

Imported from services.capability_view, NOT from gateway. gateway pulls in
FastAPI, redis and motor, and CI's coverage stage installs only pytest and its
plugins — so importing it there fails, this file is never collected, and Sonar
reports 0% coverage on new code. That reads as "untested" when the real cause is
a missing dependency, so the function lives somewhere importable on its own.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.capability_view import capability_view as _capability_view


def test_read_actions_are_left_out():
    """Every connector has ~20 of these. Carrying them would multiply a 263KB
    response for data no caller wants."""
    view = _capability_view(
        [
            {"id": "sync", "method": "GET"},
            {"id": "list_messages", "method": "GET"},
            {"id": "send_sms", "method": "POST", "capability": "sms.send"},
        ]
    )
    assert view["capabilities"] == ["sms.send"]
    assert [a["action"] for a in view["capability_actions"]] == ["send_sms"]


def test_the_field_map_survives():
    """The map is what lets a node switch Twilio → Plivo without being retyped.
    Losing it turns a swappable node into a hardcoded one."""
    view = _capability_view(
        [
            {
                "id": "send_sms",
                "method": "POST",
                "capability": "sms.send",
                "capability_map": {"to": "dst", "body": "text", "from": "src"},
                "params": [{"key": "dst", "required": True}],
            }
        ]
    )
    action = view["capability_actions"][0]
    assert action["map"] == {"to": "dst", "body": "text", "from": "src"}
    assert action["params"] == [{"key": "dst", "required": True}]
    assert action["opaque"] is False


def test_an_opaque_action_is_flagged_not_hidden():
    """Vonage and Bandwidth take one undocumented `payload` object. They should
    still be listed — a caller may know the shape — but a node cannot offer
    portable fields for them, so the flag has to reach the UI."""
    view = _capability_view([{"id": "send_sms", "capability": "sms.send", "capability_opaque": True}])
    action = view["capability_actions"][0]
    assert action["opaque"] is True
    assert action["map"] is None


def test_capabilities_are_deduped_and_sorted():
    """Plivo declares sms.send twice — send_sms and send_mms. The picker must
    show Plivo once while both actions stay selectable."""
    view = _capability_view(
        [
            {"id": "send_mms", "capability": "sms.send"},
            {"id": "send_sms", "capability": "sms.send"},
            {"id": "create_event", "capability": "calendar.create_event"},
        ]
    )
    assert view["capabilities"] == ["calendar.create_event", "sms.send"]
    assert len(view["capability_actions"]) == 3


def test_a_connector_with_nothing_to_offer():
    """Most of the 213 are ingestion-only. They must project empty rather than
    absent, so callers can rely on the keys being there."""
    for apis in (None, [], [{"id": "sync", "method": "GET"}]):
        view = _capability_view(apis)
        assert view["capabilities"] == []
        assert view["capability_actions"] == []


def test_junk_in_the_catalog_does_not_break_the_listing():
    """Catalog docs are seeded from per-connector JSON written by many hands; one
    malformed entry must not take out the whole catalog response."""
    view = _capability_view(["not-a-dict", None, {"id": "send_sms", "capability": "sms.send"}])
    assert view["capabilities"] == ["sms.send"]


def test_label_falls_back_to_the_action_id():
    """The picker shows this. An unnamed action should read as its id, not blank."""
    view = _capability_view([{"id": "send_sms", "capability": "sms.send"}])
    assert view["capability_actions"][0]["label"] == "send_sms"
