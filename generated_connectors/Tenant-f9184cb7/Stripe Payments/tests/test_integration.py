"""
Integration tests for StripeConnector — requires a real Stripe test-mode API key.

Run with:
    STRIPE_TEST_API_KEY=sk_test_... pytest tests/test_integration.py -m integration -v

These tests call the live Stripe API and create/delete resources in your test account.
They are skipped automatically when STRIPE_TEST_API_KEY is not set.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import StripeConnector
from models import AuthStatus, ConnectorHealth, SyncStatus

CONNECTOR_ID = "conn_stripe_integration"
TENANT_ID = "tenant_integration_001"

# Skip the entire module if the env var is absent
pytestmark = pytest.mark.integration

STRIPE_TEST_API_KEY = os.environ.get("STRIPE_TEST_API_KEY", "")


@pytest.fixture(scope="module")
def real_connector() -> StripeConnector:
    if not STRIPE_TEST_API_KEY:
        pytest.skip("STRIPE_TEST_API_KEY not set — skipping integration tests")
    return StripeConnector(
        api_key=STRIPE_TEST_API_KEY,
        connector_id=CONNECTOR_ID,
        tenant_id=TENANT_ID,
    )


@pytest.mark.asyncio
async def test_integration_install(real_connector: StripeConnector) -> None:
    result = await real_connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_integration_health_check(real_connector: StripeConnector) -> None:
    result = await real_connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_integration_get_balance(real_connector: StripeConnector) -> None:
    result = await real_connector.get_balance()
    assert result["object"] == "balance"
    assert "available" in result


@pytest.mark.asyncio
async def test_integration_list_customers(real_connector: StripeConnector) -> None:
    result = await real_connector.list_customers(limit=5)
    assert "data" in result
    assert isinstance(result["data"], list)
    assert "has_more" in result


@pytest.mark.asyncio
async def test_integration_get_customer(real_connector: StripeConnector) -> None:
    customers = await real_connector.list_customers(limit=1)
    if not customers["data"]:
        pytest.skip("No customers in test account")
    cid = customers["data"][0]["id"]
    result = await real_connector.get_customer(cid)
    assert result["id"] == cid
    assert result["object"] == "customer"


@pytest.mark.asyncio
async def test_integration_create_delete_customer(real_connector: StripeConnector) -> None:
    created = await real_connector.create_customer(
        name="Shielva Integration Test",
        email="shielva_integration_test@example.com",
    )
    assert created["object"] == "customer"
    cid = created["id"]
    # Cleanup
    deleted = await real_connector.delete_customer(cid)
    assert deleted.get("deleted") is True


@pytest.mark.asyncio
async def test_integration_list_charges(real_connector: StripeConnector) -> None:
    result = await real_connector.list_charges(limit=5)
    assert "data" in result
    assert isinstance(result["data"], list)


@pytest.mark.asyncio
async def test_integration_list_payment_intents(real_connector: StripeConnector) -> None:
    result = await real_connector.list_payment_intents(limit=5)
    assert "data" in result


@pytest.mark.asyncio
async def test_integration_list_subscriptions(real_connector: StripeConnector) -> None:
    result = await real_connector.list_subscriptions(limit=5)
    assert "data" in result


@pytest.mark.asyncio
async def test_integration_list_products(real_connector: StripeConnector) -> None:
    result = await real_connector.list_products(limit=5, active=True)
    assert "data" in result


@pytest.mark.asyncio
async def test_integration_list_invoices(real_connector: StripeConnector) -> None:
    result = await real_connector.list_invoices(limit=5)
    assert "data" in result


@pytest.mark.asyncio
async def test_integration_list_events(real_connector: StripeConnector) -> None:
    result = await real_connector.list_events(limit=10)
    assert "data" in result
    for evt in result["data"]:
        assert "type" in evt
        assert "id" in evt


@pytest.mark.asyncio
async def test_integration_incremental_sync(real_connector: StripeConnector) -> None:
    since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    result = await real_connector.sync(since=since)
    assert result.status in (SyncStatus.COMPLETED, SyncStatus.PARTIAL)
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_integration_list_webhooks(real_connector: StripeConnector) -> None:
    result = await real_connector.list_webhooks()
    assert "data" in result
