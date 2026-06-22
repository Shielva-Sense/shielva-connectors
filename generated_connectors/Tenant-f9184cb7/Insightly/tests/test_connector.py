"""Unit tests for InsightlyConnector — respx-mocked, zero real I/O."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, SyncStatus

from connector import InsightlyConnector, normalize_contact as _real_normalize_contact
from exceptions import (
    InsightlyAuthError,
    InsightlyBadRequestError,
    InsightlyConflictError,
    InsightlyError,
    InsightlyNotFound,
    InsightlyNotFoundError,
    InsightlyRateLimitError,
)

from tests.conftest import (
    CONNECTOR_ID,
    TENANT_ID,
    TEST_API_KEY,
    TEST_BASE,
    TEST_CONFIG,
    TEST_POD,
)


# ═══════════════════════════════════════════════════════════════════════════
# Class identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_class_attr() -> None:
    assert InsightlyConnector.CONNECTOR_TYPE == "insightly"


def test_auth_type_class_attr() -> None:
    assert InsightlyConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_defined() -> None:
    assert hasattr(InsightlyConnector, "REQUIRED_CONFIG_KEYS")
    assert "api_key" in InsightlyConnector.REQUIRED_CONFIG_KEYS
    assert "pod" in InsightlyConnector.REQUIRED_CONFIG_KEYS


def test_status_map_defined() -> None:
    assert hasattr(InsightlyConnector, "_STATUS_MAP")
    assert 401 in InsightlyConnector._STATUS_MAP
    assert 403 in InsightlyConnector._STATUS_MAP
    assert 429 in InsightlyConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector: InsightlyConnector) -> None:
    result = await connector.install()
    assert result.connector_id == CONNECTOR_ID
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.AUTHENTICATED


@pytest.mark.asyncio
async def test_install_missing_api_key() -> None:
    c = InsightlyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"pod": TEST_POD},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_pod() -> None:
    c = InsightlyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": TEST_API_KEY, "pod": ""},
    )
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# Pod-aware base URL
# ═══════════════════════════════════════════════════════════════════════════


def test_default_pod_is_na1() -> None:
    c = InsightlyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": TEST_API_KEY, "pod": ""},
    )
    assert c.pod == "na1"
    assert "api.na1.insightly.com" in c.base_url


def test_eu1_pod_yields_eu1_base() -> None:
    c = InsightlyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": TEST_API_KEY, "pod": "eu1"},
    )
    assert c.base_url == "https://api.eu1.insightly.com/v3.1"


@respx.mock
@pytest.mark.asyncio
async def test_eu1_pod_actually_hits_eu1_base() -> None:
    """Pod-aware URL must end up on the wire."""
    c = InsightlyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": TEST_API_KEY, "pod": "eu1"},
    )
    route = respx.get("https://api.eu1.insightly.com/v3.1/Users/Me").mock(
        return_value=httpx.Response(200, json={"USER_ID": 1})
    )
    result = await c.health_check()
    assert route.called
    assert result.health == ConnectorHealth.HEALTHY


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape (Basic, api_key:empty) + auth-error path
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_basic_api_key_colon_empty(
    connector: InsightlyConnector,
) -> None:
    """Insightly Basic auth = base64(api_key + ":") with no password."""
    route = respx.get(f"{TEST_BASE}/Users/Me").mock(
        return_value=httpx.Response(200, json={"USER_ID": 1})
    )
    await connector.http_client.get_me()
    auth = route.calls.last.request.headers.get("authorization", "")
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
    assert decoded == f"{TEST_API_KEY}:"


@respx.mock
@pytest.mark.asyncio
async def test_auth_error_401_raises_insightly_auth_error(
    connector: InsightlyConnector,
) -> None:
    respx.get(f"{TEST_BASE}/Users/Me").mock(
        return_value=httpx.Response(401, json={"Message": "Invalid API key"})
    )
    with pytest.raises(InsightlyAuthError):
        await connector.http_client.get_me()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Users/Me").mock(
        return_value=httpx.Response(200, json={"USER_ID": 1, "EMAIL_ADDRESS": "me@example.com"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_auth_error(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Users/Me").mock(
        return_value=httpx.Response(401, json={"Message": "Invalid API key"})
    )
    result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_server_error_offline(
    connector: InsightlyConnector, no_retry_sleep
) -> None:
    respx.get(f"{TEST_BASE}/Users/Me").mock(
        return_value=httpx.Response(503, json={"Message": "Service unavailable"})
    )
    result = await connector.health_check()
    # 503 → server error retried × 3 → InsightlyServerError (== InsightlyNetworkError)
    # which the health_check catches as InsightlyNetworkError → OFFLINE.
    assert result.health == ConnectorHealth.OFFLINE


# ═══════════════════════════════════════════════════════════════════════════
# authorize() — no-op api_key surface
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authorize_returns_api_key_as_token(
    connector: InsightlyConnector,
) -> None:
    token = await connector.authorize(auth_code="", state="")
    assert token.access_token == TEST_API_KEY
    assert token.token_type == "api_key"
    assert token.refresh_token is None


# ═══════════════════════════════════════════════════════════════════════════
# Contacts
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_first_page(connector: InsightlyConnector) -> None:
    payload = [
        {"CONTACT_ID": 1, "FIRST_NAME": "Alice", "LAST_NAME": "Doe"},
        {"CONTACT_ID": 2, "FIRST_NAME": "Bob", "LAST_NAME": "Roe"},
    ]
    route = respx.get(f"{TEST_BASE}/Contacts").mock(
        return_value=httpx.Response(200, json=payload)
    )
    result = await connector.list_contacts(top=50, skip=0)
    assert route.called
    assert len(result) == 2
    qs = route.calls.last.request.url.params
    assert qs["top"] == "50"
    assert qs["skip"] == "0"


@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_pagination(connector: InsightlyConnector) -> None:
    route = respx.get(f"{TEST_BASE}/Contacts").mock(
        return_value=httpx.Response(200, json=[])
    )
    await connector.list_contacts(top=200, skip=400)
    qs = route.calls.last.request.url.params
    assert qs["top"] == "200"
    assert qs["skip"] == "400"


@respx.mock
@pytest.mark.asyncio
async def test_get_contact_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Contacts/42").mock(
        return_value=httpx.Response(200, json={"CONTACT_ID": 42, "FIRST_NAME": "Eve"})
    )
    result = await connector.get_contact(42)
    assert result["CONTACT_ID"] == 42


@respx.mock
@pytest.mark.asyncio
async def test_get_contact_not_found_raises(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Contacts/9999").mock(
        return_value=httpx.Response(404, json={"Message": "Not found"})
    )
    with pytest.raises(InsightlyNotFound):
        await connector.get_contact(9999)


@respx.mock
@pytest.mark.asyncio
async def test_create_contact_posts_payload(connector: InsightlyConnector) -> None:
    route = respx.post(f"{TEST_BASE}/Contacts").mock(
        return_value=httpx.Response(
            201, json={"CONTACT_ID": 1001, "FIRST_NAME": "Carol"}
        )
    )
    result = await connector.create_contact(
        first_name="Carol", last_name="Lee", email="carol@example.com"
    )
    assert route.called
    body = route.calls.last.request.content.decode("utf-8")
    assert "Carol" in body
    assert "carol@example.com" in body
    assert result["CONTACT_ID"] == 1001


@respx.mock
@pytest.mark.asyncio
async def test_create_contact_includes_phone_block(
    connector: InsightlyConnector,
) -> None:
    route = respx.post(f"{TEST_BASE}/Contacts").mock(
        return_value=httpx.Response(201, json={"CONTACT_ID": 1002})
    )
    await connector.create_contact(first_name="Dan", phone="+1 555 1234")
    body = route.calls.last.request.content.decode("utf-8")
    assert "CONTACTINFOS" in body
    assert "+1 555 1234" in body


@respx.mock
@pytest.mark.asyncio
async def test_update_contact_includes_id_in_body(
    connector: InsightlyConnector,
) -> None:
    route = respx.put(f"{TEST_BASE}/Contacts/77").mock(
        return_value=httpx.Response(200, json={"CONTACT_ID": 77, "FIRST_NAME": "Updated"})
    )
    result = await connector.update_contact(77, {"FIRST_NAME": "Updated"})
    body = route.calls.last.request.content.decode("utf-8")
    assert '"CONTACT_ID": 77' in body or '"CONTACT_ID":77' in body
    assert result["FIRST_NAME"] == "Updated"


@respx.mock
@pytest.mark.asyncio
async def test_delete_contact_ok(connector: InsightlyConnector) -> None:
    respx.delete(f"{TEST_BASE}/Contacts/55").mock(return_value=httpx.Response(202))
    result = await connector.delete_contact(55)
    assert result == {"deleted": 55}


@respx.mock
@pytest.mark.asyncio
async def test_delete_contact_already_gone(connector: InsightlyConnector) -> None:
    respx.delete(f"{TEST_BASE}/Contacts/55").mock(
        return_value=httpx.Response(404, json={"Message": "Gone"})
    )
    result = await connector.delete_contact(55)
    assert result == {"deleted": 55, "already_missing": True}


# ═══════════════════════════════════════════════════════════════════════════
# Organisations
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_organisations_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Organisations").mock(
        return_value=httpx.Response(200, json=[{"ORGANISATION_ID": 7, "ORGANISATION_NAME": "Acme"}])
    )
    result = await connector.list_organisations()
    assert result[0]["ORGANISATION_NAME"] == "Acme"


@respx.mock
@pytest.mark.asyncio
async def test_get_organisation_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Organisations/7").mock(
        return_value=httpx.Response(200, json={"ORGANISATION_ID": 7})
    )
    result = await connector.get_organisation(7)
    assert result["ORGANISATION_ID"] == 7


@respx.mock
@pytest.mark.asyncio
async def test_create_organisation_ok(connector: InsightlyConnector) -> None:
    route = respx.post(f"{TEST_BASE}/Organisations").mock(
        return_value=httpx.Response(201, json={"ORGANISATION_ID": 9, "ORGANISATION_NAME": "NewCo"})
    )
    result = await connector.create_organisation(
        organisation_name="NewCo", phone="+1", website="https://newco.example"
    )
    assert route.called
    body = route.calls.last.request.content.decode("utf-8")
    assert "NewCo" in body
    assert "https://newco.example" in body
    assert result["ORGANISATION_ID"] == 9


@pytest.mark.asyncio
async def test_create_organisation_requires_name(
    connector: InsightlyConnector,
) -> None:
    with pytest.raises(ValueError, match="organisation_name"):
        await connector.create_organisation(organisation_name="")


@respx.mock
@pytest.mark.asyncio
async def test_update_organisation_ok(connector: InsightlyConnector) -> None:
    respx.put(f"{TEST_BASE}/Organisations/7").mock(
        return_value=httpx.Response(200, json={"ORGANISATION_ID": 7, "PHONE": "+9"})
    )
    result = await connector.update_organisation(7, {"PHONE": "+9"})
    assert result["PHONE"] == "+9"


@respx.mock
@pytest.mark.asyncio
async def test_delete_organisation_ok(connector: InsightlyConnector) -> None:
    respx.delete(f"{TEST_BASE}/Organisations/7").mock(return_value=httpx.Response(202))
    result = await connector.delete_organisation(7)
    assert result == {"deleted": 7}


# ═══════════════════════════════════════════════════════════════════════════
# Opportunities
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_opportunities_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Opportunities").mock(
        return_value=httpx.Response(200, json=[{"OPPORTUNITY_ID": 1, "OPPORTUNITY_NAME": "Deal A"}])
    )
    result = await connector.list_opportunities(top=10)
    assert result[0]["OPPORTUNITY_NAME"] == "Deal A"


@respx.mock
@pytest.mark.asyncio
async def test_get_opportunity_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Opportunities/1").mock(
        return_value=httpx.Response(200, json={"OPPORTUNITY_ID": 1})
    )
    result = await connector.get_opportunity(1)
    assert result["OPPORTUNITY_ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_create_opportunity_ok(connector: InsightlyConnector) -> None:
    route = respx.post(f"{TEST_BASE}/Opportunities").mock(
        return_value=httpx.Response(
            201,
            json={
                "OPPORTUNITY_ID": 99,
                "OPPORTUNITY_NAME": "Q3 Renewal",
                "OPPORTUNITY_VALUE": 12500.0,
                "PROBABILITY": 75,
                "BID_CURRENCY": "USD",
            },
        )
    )
    result = await connector.create_opportunity(
        opportunity_name="Q3 Renewal", opportunity_value=12500.0, probability=75
    )
    assert route.called
    assert result["OPPORTUNITY_ID"] == 99


@pytest.mark.asyncio
async def test_create_opportunity_requires_name(
    connector: InsightlyConnector,
) -> None:
    with pytest.raises(ValueError, match="opportunity_name"):
        await connector.create_opportunity(opportunity_name="")


@pytest.mark.asyncio
async def test_create_opportunity_rejects_bad_probability(
    connector: InsightlyConnector,
) -> None:
    with pytest.raises(ValueError, match="probability"):
        await connector.create_opportunity(opportunity_name="Bad", probability=150)


@respx.mock
@pytest.mark.asyncio
async def test_update_opportunity_ok(connector: InsightlyConnector) -> None:
    respx.put(f"{TEST_BASE}/Opportunities/9").mock(
        return_value=httpx.Response(200, json={"OPPORTUNITY_ID": 9, "PROBABILITY": 90})
    )
    result = await connector.update_opportunity(9, {"PROBABILITY": 90})
    assert result["PROBABILITY"] == 90


@respx.mock
@pytest.mark.asyncio
async def test_delete_opportunity_ok(connector: InsightlyConnector) -> None:
    respx.delete(f"{TEST_BASE}/Opportunities/9").mock(return_value=httpx.Response(202))
    result = await connector.delete_opportunity(9)
    assert result == {"deleted": 9}


# ═══════════════════════════════════════════════════════════════════════════
# Leads
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_leads_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Leads").mock(
        return_value=httpx.Response(200, json=[{"LEAD_ID": 1}])
    )
    result = await connector.list_leads()
    assert result[0]["LEAD_ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_get_lead_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Leads/1").mock(
        return_value=httpx.Response(200, json={"LEAD_ID": 1})
    )
    result = await connector.get_lead(1)
    assert result["LEAD_ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_create_lead_ok(connector: InsightlyConnector) -> None:
    route = respx.post(f"{TEST_BASE}/Leads").mock(
        return_value=httpx.Response(201, json={"LEAD_ID": 11, "EMAIL": "x@e.com"})
    )
    result = await connector.create_lead(
        first_name="Pat", last_name="Lee", email="x@e.com", lead_source_id=3
    )
    body = route.calls.last.request.content.decode("utf-8")
    assert "Pat" in body
    assert "LEAD_SOURCE_ID" in body
    assert result["LEAD_ID"] == 11


@respx.mock
@pytest.mark.asyncio
async def test_update_lead_ok(connector: InsightlyConnector) -> None:
    respx.put(f"{TEST_BASE}/Leads/11").mock(
        return_value=httpx.Response(200, json={"LEAD_ID": 11, "FIRST_NAME": "Pat2"})
    )
    result = await connector.update_lead(11, {"FIRST_NAME": "Pat2"})
    assert result["FIRST_NAME"] == "Pat2"


@respx.mock
@pytest.mark.asyncio
async def test_delete_lead_ok(connector: InsightlyConnector) -> None:
    respx.delete(f"{TEST_BASE}/Leads/11").mock(return_value=httpx.Response(202))
    result = await connector.delete_lead(11)
    assert result == {"deleted": 11}


# ═══════════════════════════════════════════════════════════════════════════
# Projects
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_projects_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Projects").mock(
        return_value=httpx.Response(200, json=[{"PROJECT_ID": 1}])
    )
    result = await connector.list_projects()
    assert result[0]["PROJECT_ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_create_project_ok(connector: InsightlyConnector) -> None:
    route = respx.post(f"{TEST_BASE}/Projects").mock(
        return_value=httpx.Response(201, json={"PROJECT_ID": 5, "PROJECT_NAME": "Migration"})
    )
    result = await connector.create_project(project_name="Migration")
    body = route.calls.last.request.content.decode("utf-8")
    assert "Migration" in body
    assert "STATUS" in body
    assert result["PROJECT_ID"] == 5


@pytest.mark.asyncio
async def test_create_project_requires_name(connector: InsightlyConnector) -> None:
    with pytest.raises(ValueError, match="project_name"):
        await connector.create_project(project_name="")


@respx.mock
@pytest.mark.asyncio
async def test_update_project_ok(connector: InsightlyConnector) -> None:
    respx.put(f"{TEST_BASE}/Projects/5").mock(
        return_value=httpx.Response(200, json={"PROJECT_ID": 5, "STATUS": "Completed"})
    )
    result = await connector.update_project(5, {"STATUS": "Completed"})
    assert result["STATUS"] == "Completed"


@respx.mock
@pytest.mark.asyncio
async def test_delete_project_ok(connector: InsightlyConnector) -> None:
    respx.delete(f"{TEST_BASE}/Projects/5").mock(return_value=httpx.Response(202))
    result = await connector.delete_project(5)
    assert result == {"deleted": 5}


# ═══════════════════════════════════════════════════════════════════════════
# Tasks
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_tasks_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Tasks").mock(
        return_value=httpx.Response(200, json=[{"TASK_ID": 1}])
    )
    result = await connector.list_tasks()
    assert result[0]["TASK_ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_create_task_ok(connector: InsightlyConnector) -> None:
    route = respx.post(f"{TEST_BASE}/Tasks").mock(
        return_value=httpx.Response(201, json={"TASK_ID": 12, "TITLE": "Email customer"})
    )
    result = await connector.create_task(title="Email customer", priority=1)
    body = route.calls.last.request.content.decode("utf-8")
    assert "Email customer" in body
    assert "PRIORITY" in body
    assert result["TASK_ID"] == 12


@pytest.mark.asyncio
async def test_create_task_requires_title(connector: InsightlyConnector) -> None:
    with pytest.raises(ValueError, match="title"):
        await connector.create_task(title="")


@respx.mock
@pytest.mark.asyncio
async def test_update_task_ok(connector: InsightlyConnector) -> None:
    respx.put(f"{TEST_BASE}/Tasks/12").mock(
        return_value=httpx.Response(200, json={"TASK_ID": 12, "STATUS": "Completed"})
    )
    result = await connector.update_task(12, {"STATUS": "Completed"})
    assert result["STATUS"] == "Completed"


@respx.mock
@pytest.mark.asyncio
async def test_delete_task_ok(connector: InsightlyConnector) -> None:
    respx.delete(f"{TEST_BASE}/Tasks/12").mock(return_value=httpx.Response(202))
    result = await connector.delete_task(12)
    assert result == {"deleted": 12}


# ═══════════════════════════════════════════════════════════════════════════
# Read-only surfaces (Events / Notes / Emails / Pipelines / Users / etc.)
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_events_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Events").mock(
        return_value=httpx.Response(200, json=[{"EVENT_ID": 1}])
    )
    result = await connector.list_events()
    assert result[0]["EVENT_ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_list_notes_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Notes").mock(
        return_value=httpx.Response(200, json=[{"NOTE_ID": 1}])
    )
    result = await connector.list_notes()
    assert result[0]["NOTE_ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_list_emails_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Emails").mock(
        return_value=httpx.Response(200, json=[{"EMAIL_ID": 1}])
    )
    result = await connector.list_emails()
    assert result[0]["EMAIL_ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_list_pipelines_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Pipelines").mock(
        return_value=httpx.Response(200, json=[{"PIPELINE_ID": 1}])
    )
    result = await connector.list_pipelines()
    assert result[0]["PIPELINE_ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_list_users_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Users").mock(
        return_value=httpx.Response(200, json=[{"USER_ID": 1}])
    )
    result = await connector.list_users()
    assert result[0]["USER_ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_list_custom_objects_ok(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/CustomObjects").mock(
        return_value=httpx.Response(200, json=[{"OBJECT_NAME": "PetType"}])
    )
    result = await connector.list_custom_objects()
    assert result[0]["OBJECT_NAME"] == "PetType"


@respx.mock
@pytest.mark.asyncio
async def test_list_tags_default_contacts(connector: InsightlyConnector) -> None:
    route = respx.get(f"{TEST_BASE}/Tags/contacts").mock(
        return_value=httpx.Response(200, json=[{"TAG_NAME": "vip"}])
    )
    result = await connector.list_tags()
    assert route.called
    assert result[0]["TAG_NAME"] == "vip"


@respx.mock
@pytest.mark.asyncio
async def test_list_tags_custom_record_type(connector: InsightlyConnector) -> None:
    route = respx.get(f"{TEST_BASE}/Tags/opportunities").mock(
        return_value=httpx.Response(200, json=[])
    )
    await connector.list_tags("opportunities")
    assert route.called


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 / 500 — exponential backoff converges to success
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(
    connector: InsightlyConnector, no_retry_sleep
) -> None:
    route = respx.get(f"{TEST_BASE}/Contacts").mock(
        side_effect=[
            httpx.Response(429, json={"Message": "Slow down"}),
            httpx.Response(200, json=[{"CONTACT_ID": 1}]),
        ]
    )
    result = await connector.list_contacts()
    assert route.call_count == 2
    assert result[0]["CONTACT_ID"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(
    connector: InsightlyConnector, no_retry_sleep
) -> None:
    route = respx.get(f"{TEST_BASE}/Contacts").mock(
        side_effect=[
            httpx.Response(500, json={"Message": "boom"}),
            httpx.Response(200, json=[]),
        ]
    )
    result = await connector.list_contacts()
    assert route.call_count == 2
    assert result == []


@respx.mock
@pytest.mark.asyncio
async def test_429_exhausts_retries_then_raises(
    connector: InsightlyConnector, no_retry_sleep
) -> None:
    respx.get(f"{TEST_BASE}/Contacts").mock(
        return_value=httpx.Response(429, json={"Message": "Slow down"})
    )
    with pytest.raises(InsightlyRateLimitError):
        await connector.list_contacts()


# ═══════════════════════════════════════════════════════════════════════════
# Sync — uses mocked HTTPClient (NOT real httpx route)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sync_pages_contacts_and_completes(
    mock_InsightlyHTTPClient,
) -> None:
    _, mock_instance = mock_InsightlyHTTPClient
    # First page = 2 contacts, second page = empty → loop terminates.
    mock_instance.list_contacts = AsyncMock(
        side_effect=[
            [{"CONTACT_ID": 1, "FIRST_NAME": "A"}, {"CONTACT_ID": 2, "FIRST_NAME": "B"}],
            [],
        ]
    )

    c = InsightlyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
    result = await c.sync(full=True)

    assert result.status == SyncStatus.COMPLETED
    assert result.documents_found == 2
    assert result.documents_synced == 2
    assert result.documents_failed == 0


@pytest.mark.asyncio
async def test_sync_handles_normalize_failure_partial(
    mock_InsightlyHTTPClient,
    mocker,
) -> None:
    _, mock_instance = mock_InsightlyHTTPClient
    mock_instance.list_contacts = AsyncMock(
        side_effect=[
            [{"CONTACT_ID": 1, "FIRST_NAME": "A"}, {"CONTACT_ID": 2, "FIRST_NAME": "B"}],
            [],
        ]
    )
    # Force normalize_contact to raise on the second row.
    def fake_norm(raw, connector_id, tenant_id):
        if raw.get("CONTACT_ID") == 2:
            raise RuntimeError("kaboom")
        return _real_normalize_contact(raw, connector_id, tenant_id)

    mocker.patch("connector.normalize_contact", side_effect=fake_norm)

    c = InsightlyConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config=dict(TEST_CONFIG),
    )
    result = await c.sync(full=True)
    assert result.status == SyncStatus.PARTIAL
    assert result.documents_synced == 1
    assert result.documents_failed == 1


# ═══════════════════════════════════════════════════════════════════════════
# Auth-error propagation on data calls
# ═══════════════════════════════════════════════════════════════════════════


@respx.mock
@pytest.mark.asyncio
async def test_list_contacts_auth_error(connector: InsightlyConnector) -> None:
    respx.get(f"{TEST_BASE}/Contacts").mock(
        return_value=httpx.Response(401, json={"Message": "bad key"})
    )
    with pytest.raises(InsightlyAuthError):
        await connector.list_contacts()


@respx.mock
@pytest.mark.asyncio
async def test_400_raises_bad_request(connector: InsightlyConnector) -> None:
    respx.post(f"{TEST_BASE}/Contacts").mock(
        return_value=httpx.Response(400, json={"Message": "bad payload"})
    )
    with pytest.raises(InsightlyBadRequestError):
        await connector.create_contact(first_name="X")


@respx.mock
@pytest.mark.asyncio
async def test_409_raises_conflict(connector: InsightlyConnector) -> None:
    respx.post(f"{TEST_BASE}/Contacts").mock(
        return_value=httpx.Response(409, json={"Message": "dup"})
    )
    with pytest.raises(InsightlyConflictError):
        await connector.create_contact(first_name="X")


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


def test_independent_instances_per_tenant() -> None:
    c1 = InsightlyConnector("t-A", "c-A", dict(TEST_CONFIG))
    c2 = InsightlyConnector("t-B", "c-B", dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id


def test_normalize_contact_namespaces_tenant() -> None:
    from helpers.normalizer import normalize_contact

    raw = {"CONTACT_ID": 42, "FIRST_NAME": "Ada", "LAST_NAME": "Lovelace"}
    a = normalize_contact(raw, "conn-A", "tenant-A")
    b = normalize_contact(raw, "conn-B", "tenant-B")
    assert a.id.startswith("tenant-A_")
    assert b.id.startswith("tenant-B_")
    assert a.id != b.id
    assert a.source_id == b.source_id == "42"
