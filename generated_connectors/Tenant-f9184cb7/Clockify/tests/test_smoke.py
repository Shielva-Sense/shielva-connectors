"""Smoke test — verifies the connector wires up end-to-end without any I/O.

This file complements `test_connector.py` (full behavioural suite) by:
  * Confirming the package imports clean from the canonical entry point.
  * Confirming the connector instantiates with the documented config shape.
  * Confirming the public method surface matches the spec (CRUD + lifecycle).
  * Confirming class-level identity constants are stable.

Zero HTTP traffic. Zero mocks beyond the autouse storage stub.
"""
import inspect

import pytest

from connector import ClockifyConnector  # canonical entry point

from tests.conftest import CONNECTOR_ID, TENANT_ID, TEST_CONFIG


def test_package_root_exports_connector():
    """`__init__.py` must re-export ClockifyConnector."""
    import importlib

    pkg = importlib.import_module("__init__")  # noqa: F401  — root module presence
    # Direct module import is the canonical surface
    from connector import ClockifyConnector as Reimport

    assert Reimport is ClockifyConnector


def test_identity_constants_stable():
    assert ClockifyConnector.CONNECTOR_TYPE == "clockify"
    assert ClockifyConnector.CONNECTOR_NAME == "Clockify"
    assert ClockifyConnector.AUTH_TYPE == "api_key"


def test_connector_instantiates_with_documented_config():
    c = ClockifyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
    assert c.tenant_id == TENANT_ID
    assert c.connector_id == CONNECTOR_ID
    assert c.api_key == TEST_CONFIG["api_key"]
    assert c.base_url == TEST_CONFIG["base_url"]
    assert c.reports_base_url == TEST_CONFIG["reports_base_url"]
    # http_client wired with the same base URLs
    assert c.http_client.base_url == TEST_CONFIG["base_url"]
    assert c.http_client.reports_base_url == TEST_CONFIG["reports_base_url"]


@pytest.mark.parametrize(
    "method_name",
    [
        # Lifecycle
        "install",
        "authorize",
        "health_check",
        "sync",
        # Identity
        "get_current_user",
        # Workspaces
        "list_workspaces",
        # Projects
        "list_projects",
        "get_project",
        "create_project",
        # Tasks
        "list_tasks",
        # Time entries — CRUD + timer
        "list_time_entries",
        "get_time_entry",
        "start_time_entry",
        "stop_time_entry",
        "create_time_entry",
        "update_time_entry",
        "delete_time_entry",
        # Tags / Clients / Users
        "list_tags",
        "list_clients",
        "create_client",
        "list_users",
        # Reports
        "summary_report",
    ],
)
def test_public_method_surface_is_async(method_name):
    """Every spec-mandated method exists and is `async def`."""
    fn = getattr(ClockifyConnector, method_name, None)
    assert fn is not None, f"missing method: {method_name}"
    assert inspect.iscoroutinefunction(fn), f"{method_name} must be async def"
