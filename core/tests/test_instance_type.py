"""A call by the workspace's own instance id must resolve to its TYPE.

Otherwise the upgrade check on the test route found "no wheel in manifest" and a
published connector fix never reached anyone calling by id.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

gw = pytest.importorskip("gateway", reason="gateway import needs the CI dependency set")

T = "Tenant-90de08d4"


def test_the_workspace_instance_names_its_type() -> None:
    assert gw._instance_type(f"canonical_google_gmail_connector_{T}", T) == "google_gmail_connector"


def test_a_persons_instance_names_its_type() -> None:
    pid = f"canonical_google_calendar_{T}__person_3f9a1c0b7d2e4a61"
    assert gw._instance_type(pid, T) == "google_calendar"


def test_another_workspaces_id_is_not_parsed_as_this_ones() -> None:
    other = "canonical_google_gmail_connector_Tenant-2"
    assert gw._instance_type(other, T) == other


def test_a_type_is_left_alone() -> None:
    assert gw._instance_type("google_gmail_connector", T) == "google_gmail_connector"
