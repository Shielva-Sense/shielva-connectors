"""Respx-mocked unit tests for PersonioConnector.

Covers:
  - install + missing creds
  - authenticate (Authorization response header capture, body fallback)
  - list_employees pagination + email filter
  - get_employee, update_employee envelope, create_employee envelope
  - list/create time_offs, attendances, time_off_types
  - list_departments / offices / projects / custom_attributes
  - list_applications / get_applicant
  - upload_document multipart
  - token rotation on response
  - 401-stale-token auto re-auth
  - 429-with-retry-after, 5xx-with-backoff
  - error mapping (404 → NotFound, 400 → BadRequest, 403 → AuthError, 409 → Conflict)
  - normalize_employee → NormalizedDocument shape
  - multi-tenant isolation
"""
from __future__ import annotations

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth, NormalizedDocument

from connector import PersonioConnector
from exceptions import (
    PersonioAuthError,
    PersonioBadRequestError,
    PersonioConflictError,
    PersonioNotFoundError,
)
from helpers.normalizer import normalize_employee

from tests.conftest import (
    BASE_URL,
    CONNECTOR_ID,
    SAMPLE_EMPLOYEE,
    TENANT_ID,
    TEST_CLIENT_ID,
    TEST_CONFIG,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _auth_route(respx_mock, token: str = "token-v1"):
    return respx_mock.post(f"{BASE_URL}/auth").mock(
        return_value=httpx.Response(
            200,
            headers={"Authorization": f"Bearer {token}"},
            json={"success": True, "data": {"token": token}},
        )
    )


# ═══════════════════════════════════════════════════════════════════════════
# install()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_success(connector):
    result = await connector.install()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.PENDING
    assert result.connector_id == CONNECTOR_ID


@pytest.mark.asyncio
async def test_install_missing_client_id(connector):
    connector.config.pop("client_id", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_client_secret(connector):
    connector.config.pop("client_secret", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# authenticate()
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_authenticate_captures_token_from_authorization_response_header(
    connector,
):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r, token="initial-jwt")
        token_info = await connector.authenticate()
    assert token_info.access_token == "initial-jwt"
    assert connector.http_client.current_token() == "initial-jwt"


@pytest.mark.asyncio
async def test_authenticate_falls_back_to_body_token_when_header_missing(
    connector,
):
    """When the Authorization response header is absent, body.data.token wins."""
    with respx.mock(assert_all_called=True) as r:
        r.post(f"{BASE_URL}/auth").mock(
            return_value=httpx.Response(
                200,
                json={"success": True, "data": {"token": "body-only-jwt"}},
            )
        )
        token_info = await connector.authenticate()
    assert token_info.access_token == "body-only-jwt"


@pytest.mark.asyncio
async def test_authenticate_401_raises_personio_auth_error(connector):
    with respx.mock() as r:
        r.post(f"{BASE_URL}/auth").mock(
            return_value=httpx.Response(401, json={"error": "invalid_client"})
        )
        with pytest.raises(PersonioAuthError):
            await connector.authenticate()


@pytest.mark.asyncio
async def test_authorize_delegates_to_authenticate(connector):
    """The gateway-facing `authorize()` must mint a token like authenticate()."""
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r, token="auth-from-authorize")
        token_info = await connector.authorize(auth_code="ignored", state="ignored")
    assert token_info.access_token == "auth-from-authorize"


# ═══════════════════════════════════════════════════════════════════════════
# Auth header shape — Bearer prefix on outbound requests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_outbound_authorization_header_uses_bearer_prefix(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r, token="bearer-test")
        list_route = r.get(f"{BASE_URL}/company/employees").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer rotated"},
                json={"data": []},
            )
        )
        await connector.list_employees(limit=1)
    sent_auth = list_route.calls.last.request.headers.get("Authorization")
    assert sent_auth == "Bearer bearer-test"


# ═══════════════════════════════════════════════════════════════════════════
# list_employees — pagination + email filter
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_employees_with_pagination_and_filter(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r, token="t1")
        list_route = r.get(f"{BASE_URL}/company/employees").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer rotated-t2"},
                json={"data": [SAMPLE_EMPLOYEE], "limit": 50, "offset": 100},
            )
        )
        result = await connector.list_employees(
            limit=50, offset=100, email="ada@example.com"
        )

    assert result["data"][0]["attributes"]["first_name"]["value"] == "Ada"
    req = list_route.calls.last.request
    params = dict(httpx.QueryParams(req.url.query.decode()))
    assert params["limit"] == "50"
    assert params["offset"] == "100"
    assert params["email"] == "ada@example.com"


# ═══════════════════════════════════════════════════════════════════════════
# get_employee
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_employee_returns_raw_dict(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/company/employees/42").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": SAMPLE_EMPLOYEE},
            )
        )
        result = await connector.get_employee(42)
    assert result["data"]["attributes"]["email"]["value"] == "ada@example.com"


@pytest.mark.asyncio
async def test_get_employee_404_raises_not_found(connector):
    with respx.mock() as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/company/employees/9999").mock(
            return_value=httpx.Response(
                404,
                headers={"Authorization": "Bearer t2"},
                json={"error": "not found"},
            )
        )
        with pytest.raises(PersonioNotFoundError):
            await connector.get_employee(9999)


# ═══════════════════════════════════════════════════════════════════════════
# update_employee + create_employee — envelope shape
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_employee_sends_patch_with_attribute_envelope(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        patch_route = r.patch(f"{BASE_URL}/company/employees/42").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"success": True},
            )
        )
        result = await connector.update_employee(42, {"position": "CTO"})
    assert result == {"success": True}
    body = patch_route.calls.last.request.content.decode()
    import json as _json

    parsed = _json.loads(body)
    assert parsed == {"employee": {"attributes": {"position": "CTO"}}}


@pytest.mark.asyncio
async def test_create_employee_sends_attribute_envelope(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        post_route = r.post(f"{BASE_URL}/company/employees").mock(
            return_value=httpx.Response(
                201,
                headers={"Authorization": "Bearer t2"},
                json={"success": True, "data": {"id": 1234}},
            )
        )
        result = await connector.create_employee(
            first_name="Grace",
            last_name="Hopper",
            email="grace@example.com",
            hire_date="2026-06-01",
            department="Engineering",
            position="Admiral",
        )
    import json as _json

    body = _json.loads(post_route.calls.last.request.content.decode())
    assert body["employee"]["attributes"]["first_name"] == "Grace"
    assert body["employee"]["attributes"]["department"] == "Engineering"
    assert body["employee"]["attributes"]["email"] == "grace@example.com"
    assert result["data"]["id"] == 1234


@pytest.mark.asyncio
async def test_create_employee_400_raises_bad_request(connector):
    with respx.mock() as r:
        _auth_route(r)
        r.post(f"{BASE_URL}/company/employees").mock(
            return_value=httpx.Response(
                400,
                headers={"Authorization": "Bearer t2"},
                json={"error": "email already exists in tenant"},
            )
        )
        with pytest.raises(PersonioBadRequestError):
            await connector.create_employee(
                first_name="x", last_name="y", email="z@z.z", hire_date="2026-01-01"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Time-offs
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_time_offs_with_date_range(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        list_route = r.get(f"{BASE_URL}/company/time-offs").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": []},
            )
        )
        await connector.list_time_offs(
            start_date="2026-06-01", end_date="2026-06-30"
        )
    params = dict(
        httpx.QueryParams(list_route.calls.last.request.url.query.decode())
    )
    assert params["start_date"] == "2026-06-01"
    assert params["end_date"] == "2026-06-30"


@pytest.mark.asyncio
async def test_create_time_off_posts_full_payload(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        post_route = r.post(f"{BASE_URL}/company/time-offs").mock(
            return_value=httpx.Response(
                201,
                headers={"Authorization": "Bearer t2"},
                json={"data": {"id": 999}},
            )
        )
        result = await connector.create_time_off(
            employee_id=42,
            time_off_type_id=1,
            start_date="2026-07-01",
            end_date="2026-07-05",
            half_day_start=True,
        )
    assert result["data"]["id"] == 999
    import json as _json

    body = _json.loads(post_route.calls.last.request.content.decode())
    assert body["employee_id"] == 42
    assert body["time_off_type_id"] == 1
    assert body["half_day_start"] is True
    assert body["half_day_end"] is False


@pytest.mark.asyncio
async def test_list_time_off_types_basic(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/company/time-off-types").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": [{"id": 1, "name": "Vacation"}]},
            )
        )
        result = await connector.list_time_off_types()
    assert result["data"][0]["name"] == "Vacation"


# ═══════════════════════════════════════════════════════════════════════════
# Attendances
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_attendances_basic(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/company/attendances").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": [{"id": 7}]},
            )
        )
        result = await connector.list_attendances(
            start_date="2026-06-01", end_date="2026-06-30"
        )
    assert result["data"][0]["id"] == 7


@pytest.mark.asyncio
async def test_create_attendance_posts_attendances_array(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        post_route = r.post(f"{BASE_URL}/company/attendances").mock(
            return_value=httpx.Response(
                201,
                headers={"Authorization": "Bearer t2"},
                json={"data": [{"id": 11}]},
            )
        )
        await connector.create_attendance(
            employee=42,
            date="2026-06-15",
            start_time="09:00",
            end_time="17:00",
            break_time=30,
            comment="kickoff",
        )
    import json as _json

    body = _json.loads(post_route.calls.last.request.content.decode())
    assert body["attendances"][0]["employee"] == 42
    assert body["attendances"][0]["break"] == 30
    assert body["attendances"][0]["comment"] == "kickoff"


@pytest.mark.asyncio
async def test_update_attendance_patches_attendance_id(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        patch_route = r.patch(f"{BASE_URL}/company/attendances/11").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"success": True},
            )
        )
        await connector.update_attendance(11, {"end_time": "18:00"})
    import json as _json

    body = _json.loads(patch_route.calls.last.request.content.decode())
    assert body == {"end_time": "18:00"}


# ═══════════════════════════════════════════════════════════════════════════
# Documents
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_documents_passes_employee_id_param(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        list_route = r.get(f"{BASE_URL}/company/document-categories").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": [{"id": 3, "name": "Contracts"}]},
            )
        )
        await connector.list_documents(employee_id=42, category_id=3)
    params = dict(
        httpx.QueryParams(list_route.calls.last.request.url.query.decode())
    )
    assert params["employee_id"] == "42"
    assert params["category_id"] == "3"


@pytest.mark.asyncio
async def test_upload_document_posts_multipart(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        post_route = r.post(f"{BASE_URL}/company/employees/42/documents").mock(
            return_value=httpx.Response(
                201,
                headers={"Authorization": "Bearer t2"},
                json={"success": True, "data": {"id": "doc-1"}},
            )
        )
        result = await connector.upload_document(
            employee_id=42,
            file_bytes=b"hello-pdf-bytes",
            filename="contract.pdf",
            category_id=3,
            title="Q3 Contract",
        )
    assert result["data"]["id"] == "doc-1"
    req = post_route.calls.last.request
    assert req.headers.get("content-type", "").startswith("multipart/form-data")
    # Check that the form fields landed in the body.
    body = req.content
    assert b"category_id" in body
    assert b"hello-pdf-bytes" in body
    assert b"Q3 Contract" in body


# ═══════════════════════════════════════════════════════════════════════════
# Org structure
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_departments(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/company/departments").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": [{"type": "Department", "attributes": {"name": "Eng"}}]},
            )
        )
        result = await connector.list_departments()
    assert result["data"][0]["attributes"]["name"] == "Eng"


@pytest.mark.asyncio
async def test_list_offices(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/company/offices").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": [{"id": 1, "name": "Berlin"}]},
            )
        )
        result = await connector.list_offices()
    assert result["data"][0]["name"] == "Berlin"


@pytest.mark.asyncio
async def test_list_projects(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/company/projects").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": [{"id": 7, "name": "Platform"}]},
            )
        )
        result = await connector.list_projects()
    assert result["data"][0]["name"] == "Platform"


@pytest.mark.asyncio
async def test_list_custom_attributes(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/company/employees/custom-attributes").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": [{"key": "shoe_size", "type": "integer"}]},
            )
        )
        result = await connector.list_custom_attributes()
    assert result["data"][0]["key"] == "shoe_size"


# ═══════════════════════════════════════════════════════════════════════════
# Recruitment
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_applications_with_status_filter(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        list_route = r.get(f"{BASE_URL}/recruiting/applications").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": [{"id": 1, "status": "in_progress"}]},
            )
        )
        await connector.list_applications(limit=25, offset=0, status="in_progress")
    params = dict(
        httpx.QueryParams(list_route.calls.last.request.url.query.decode())
    )
    assert params["limit"] == "25"
    assert params["status"] == "in_progress"


@pytest.mark.asyncio
async def test_get_applicant(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/recruiting/applicants/55").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": {"id": 55, "first_name": "Linus"}},
            )
        )
        result = await connector.get_applicant(55)
    assert result["data"]["first_name"] == "Linus"


# ═══════════════════════════════════════════════════════════════════════════
# Token rotation on response — every successful response advances the bearer
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_token_rotates_on_each_response(connector):
    """The Authorization header on response[N] must become the bearer for response[N+1]."""
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r, token="auth-token-initial")
        first = r.get(f"{BASE_URL}/company/employees").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer rotated-after-list"},
                json={"data": []},
            )
        )
        second = r.get(f"{BASE_URL}/company/projects").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer rotated-after-projects"},
                json={"data": []},
            )
        )

        await connector.list_employees()
        assert connector.http_client.current_token() == "rotated-after-list"

        await connector.list_projects()

    sent_auth = second.calls.last.request.headers.get("Authorization")
    assert sent_auth == "Bearer rotated-after-list"
    assert connector.http_client.current_token() == "rotated-after-projects"
    assert first.called and second.called


@pytest.mark.asyncio
async def test_rotated_token_persists_via_set_token(connector):
    """Rotation must call BaseConnector.set_token so the platform stores the new bearer."""
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r, token="auth-initial")
        r.get(f"{BASE_URL}/company/employees").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer rotated-persist-me"},
                json={"data": []},
            )
        )
        await connector.list_employees()
    # set_token is patched with AsyncMock in conftest.
    persisted_tokens = [
        call.args[0].access_token
        for call in connector.set_token.await_args_list
    ]
    assert "rotated-persist-me" in persisted_tokens


# ═══════════════════════════════════════════════════════════════════════════
# 401 stale token → auto re-auth
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stale_token_triggers_reauth(connector, no_retry_sleep):
    """A 401 mid-call clears the cache and re-runs /auth, then retries the request."""
    with respx.mock(assert_all_called=True) as r:
        auth_route = r.post(f"{BASE_URL}/auth").mock(
            side_effect=[
                httpx.Response(
                    200,
                    headers={"Authorization": "Bearer initial"},
                    json={"success": True, "data": {"token": "initial"}},
                ),
                httpx.Response(
                    200,
                    headers={"Authorization": "Bearer refreshed"},
                    json={"success": True, "data": {"token": "refreshed"}},
                ),
            ]
        )
        list_route = r.get(f"{BASE_URL}/company/employees").mock(
            side_effect=[
                httpx.Response(401, json={"error": "expired"}),
                httpx.Response(
                    200,
                    headers={"Authorization": "Bearer rotated-t3"},
                    json={"data": []},
                ),
            ]
        )
        await connector.list_employees(limit=1)
    assert auth_route.call_count == 2  # initial + re-auth
    assert list_route.call_count == 2  # first 401 + retry


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 429 — honours Retry-After
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_on_429_then_success(connector, no_retry_sleep):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        route = r.get(f"{BASE_URL}/company/employees").mock(
            side_effect=[
                httpx.Response(
                    429,
                    headers={"Retry-After": "0", "Authorization": "Bearer t2"},
                    json={"error": "rate limited"},
                ),
                httpx.Response(
                    200,
                    headers={"Authorization": "Bearer t3"},
                    json={"data": [SAMPLE_EMPLOYEE]},
                ),
            ]
        )
        result = await connector.list_employees(limit=1)
    assert route.call_count == 2
    assert result["data"][0]["attributes"]["first_name"]["value"] == "Ada"


# ═══════════════════════════════════════════════════════════════════════════
# Retry on 5xx
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_on_500_then_success(connector, no_retry_sleep):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        route = r.get(f"{BASE_URL}/company/employees").mock(
            side_effect=[
                httpx.Response(500, json={"error": "boom"}),
                httpx.Response(
                    200,
                    headers={"Authorization": "Bearer t3"},
                    json={"data": []},
                ),
            ]
        )
        result = await connector.list_employees(limit=1)
    assert route.call_count == 2
    assert result == {"data": []}


# ═══════════════════════════════════════════════════════════════════════════
# Error mapping
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_403_raises_auth_error_with_status(connector):
    with respx.mock() as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/company/employees/1").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"}),
        )
        with pytest.raises(PersonioAuthError) as excinfo:
            await connector.get_employee(1)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_409_raises_conflict(connector):
    with respx.mock() as r:
        _auth_route(r)
        r.post(f"{BASE_URL}/company/employees").mock(
            return_value=httpx.Response(
                409,
                headers={"Authorization": "Bearer t2"},
                json={"error": "email already taken"},
            )
        )
        with pytest.raises(PersonioConflictError):
            await connector.create_employee(
                first_name="A",
                last_name="B",
                email="dup@example.com",
                hire_date="2026-01-01",
            )


# ═══════════════════════════════════════════════════════════════════════════
# health_check classification
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_health_check_healthy(connector):
    with respx.mock(assert_all_called=True) as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/company/employees").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t2"},
                json={"data": [SAMPLE_EMPLOYEE]},
            )
        )
        result = await connector.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@pytest.mark.asyncio
async def test_health_check_401_degraded_token_expired(connector, no_retry_sleep):
    """Persistent 401s — even after re-auth retry — surface as DEGRADED + TOKEN_EXPIRED."""
    with respx.mock() as r:
        r.post(f"{BASE_URL}/auth").mock(
            return_value=httpx.Response(
                200,
                headers={"Authorization": "Bearer t1"},
                json={"success": True, "data": {"token": "t1"}},
            )
        )
        r.get(f"{BASE_URL}/company/employees").mock(
            return_value=httpx.Response(401, json={"error": "expired"})
        )
        result = await connector.health_check()
    assert result.health == ConnectorHealth.DEGRADED
    assert result.auth_status == AuthStatus.TOKEN_EXPIRED


@pytest.mark.asyncio
async def test_health_check_403_unhealthy_invalid_creds(connector, no_retry_sleep):
    with respx.mock() as r:
        _auth_route(r)
        r.get(f"{BASE_URL}/company/employees").mock(
            return_value=httpx.Response(403, json={"error": "no scope"})
        )
        result = await connector.health_check()
    assert result.health == ConnectorHealth.UNHEALTHY
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# normalize_employee → NormalizedDocument
# ═══════════════════════════════════════════════════════════════════════════


def test_normalize_employee_shape():
    doc = normalize_employee(SAMPLE_EMPLOYEE, CONNECTOR_ID, TENANT_ID)
    assert isinstance(doc, NormalizedDocument)
    # id is tenant-scoped per the Shielva contract
    assert doc.id == f"{TENANT_ID}_42"
    assert doc.source_id == "42"
    assert doc.title == "Ada Lovelace"
    assert "Engineering" in doc.content
    assert "ada@example.com" in doc.content
    assert doc.source == "personio"
    assert doc.tenant_id == TENANT_ID
    assert doc.connector_id == CONNECTOR_ID
    assert doc.metadata["department"] == "Engineering"
    assert doc.metadata["kind"] == "personio.employee"


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════


def test_connector_type_is_personio():
    assert PersonioConnector.CONNECTOR_TYPE == "personio"


def test_auth_type_is_api_key():
    assert PersonioConnector.AUTH_TYPE == "api_key"


def test_required_config_keys_are_minimal_and_correct():
    assert PersonioConnector.REQUIRED_CONFIG_KEYS == ["client_id", "client_secret"]


def test_status_map_defined():
    assert 401 in PersonioConnector._STATUS_MAP
    assert 403 in PersonioConnector._STATUS_MAP
    assert 429 in PersonioConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_distinct_tenants_get_distinct_instances():
    c1 = PersonioConnector(
        tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG)
    )
    c2 = PersonioConnector(
        tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG)
    )
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    # Each connector owns its own HTTP client + token cache.
    assert c1.http_client is not c2.http_client


def test_tenant_id_flows_into_normalized_document_id():
    """NormalizedDocument.id must be tenant-scoped to prevent cross-tenant collisions."""
    doc_a = normalize_employee(SAMPLE_EMPLOYEE, "conn-1", "tenant-A")
    doc_b = normalize_employee(SAMPLE_EMPLOYEE, "conn-2", "tenant-B")
    assert doc_a.id != doc_b.id
    assert doc_a.id.startswith("tenant-A_")
    assert doc_b.id.startswith("tenant-B_")
