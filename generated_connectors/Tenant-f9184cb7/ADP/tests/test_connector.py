"""Unit tests for ADPConnector — respx-mocked, zero real network I/O."""
import httpx
import pytest
import respx

from connector import ADPConnector
from exceptions import ADPAuthError, ADPNotFound
from tests.conftest import (
    CONNECTOR_ID,
    SAMPLE_PAY_STATEMENT,
    SAMPLE_WORKER,
    TENANT_ID,
    TEST_CONFIG,
)

from shared.base_connector import AuthStatus, ConnectorHealth


BASE = "https://api.adp.com"
TOKEN_URL = "https://accounts.adp.com/auth/oauth/v2/token"


def _token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": "tok-abc", "expires_in": 3600, "token_type": "Bearer"},
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
async def test_install_missing_credentials(connector):
    connector.config.pop("client_secret", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_cert_path():
    cfg = dict(TEST_CONFIG)
    cfg.pop("cert_path")
    c = ADPConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "cert_path" in result.message


# ═══════════════════════════════════════════════════════════════════════════
# authenticate() — verifies client-credentials body
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_authenticate_client_credentials_body(connector):
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content.decode()
        return _token_response()

    respx.post(TOKEN_URL).mock(side_effect=_handler)
    info = await connector.authenticate()
    assert info.access_token == "tok-abc"
    assert "grant_type=client_credentials" in captured["body"]
    assert "client_id=test-client-id" in captured["body"]
    assert "client_secret=test-client-secret" in captured["body"]
    assert "application/x-www-form-urlencoded" in captured["content_type"]


@pytest.mark.asyncio
@respx.mock
async def test_authenticate_auth_error(connector):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    with pytest.raises(ADPAuthError):
        await connector.authenticate()


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_health_check_healthy(authed):
    respx.get(f"{BASE}/hr/v2/workers").mock(
        return_value=httpx.Response(200, json={"workers": [SAMPLE_WORKER]})
    )
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


# ═══════════════════════════════════════════════════════════════════════════
# list_workers — $filter + $top forwarded
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_workers_forwards_filter_and_top(authed):
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"workers": [SAMPLE_WORKER]})

    respx.get(f"{BASE}/hr/v2/workers").mock(side_effect=_handler)
    out = await authed.list_workers(top=25, filter="workerStatus/statusCode/codeValue eq 'Active'")
    assert out["workers"][0]["associateOID"] == "G3XYZ123"
    assert captured["params"]["$top"] == "25"
    assert captured["params"]["$filter"] == "workerStatus/statusCode/codeValue eq 'Active'"


# ═══════════════════════════════════════════════════════════════════════════
# get_worker / list_employees
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_get_worker(authed):
    respx.get(f"{BASE}/hr/v2/workers/G3XYZ123").mock(
        return_value=httpx.Response(200, json={"worker": SAMPLE_WORKER})
    )
    out = await authed.get_worker("G3XYZ123")
    assert out["worker"]["associateOID"] == "G3XYZ123"


@pytest.mark.asyncio
@respx.mock
async def test_get_worker_404(authed):
    respx.get(f"{BASE}/hr/v2/workers/MISSING").mock(
        return_value=httpx.Response(404, json={"response": {"requestStatus": "NOT_FOUND"}})
    )
    with pytest.raises(ADPNotFound):
        await authed.get_worker("MISSING")


@pytest.mark.asyncio
@respx.mock
async def test_list_employees(authed):
    respx.get(f"{BASE}/hr/v2/employees").mock(
        return_value=httpx.Response(200, json={"employees": []})
    )
    out = await authed.list_employees(top=10)
    assert out == {"employees": []}


# ═══════════════════════════════════════════════════════════════════════════
# payroll endpoints
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_pay_distributions(authed):
    respx.get(
        f"{BASE}/payroll/v1/workers/G3XYZ123/pay-distributions"
    ).mock(return_value=httpx.Response(200, json={"payDistributions": []}))
    out = await authed.list_pay_distributions("G3XYZ123")
    assert out == {"payDistributions": []}


@pytest.mark.asyncio
@respx.mock
async def test_list_pay_statements(authed):
    respx.get(
        f"{BASE}/payroll/v1/workers/G3XYZ123/pay-statements"
    ).mock(return_value=httpx.Response(200, json={"payStatements": [SAMPLE_PAY_STATEMENT]}))
    out = await authed.list_pay_statements("G3XYZ123", top=5)
    assert out["payStatements"][0]["payStatementID"] == "PS123"


# ═══════════════════════════════════════════════════════════════════════════
# submit_time_off_request — verifies envelope shape
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_submit_time_off_request_envelope(authed):
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["body"] = _json.loads(request.content.decode())
        return httpx.Response(200, json={"confirmMessage": {"requestStatus": "SUBMITTED"}})

    respx.post(
        f"{BASE}/time-off/v2/workers/G3XYZ123/time-off-requests"
    ).mock(side_effect=_handler)

    out = await authed.submit_time_off_request(
        worker_aoid="G3XYZ123",
        policy_code="VAC",
        start_date="2026-07-01",
        end_date="2026-07-05",
        hours=40.0,
        comments="Beach week",
    )
    assert out["confirmMessage"]["requestStatus"] == "SUBMITTED"
    tor = captured["body"]["events"][0]["data"]["transform"]["timeOffRequest"]
    assert tor["timeOffPolicyCode"]["codeValue"] == "VAC"
    assert tor["startDate"] == "2026-07-01"
    assert tor["endDate"] == "2026-07-05"
    assert tor["totalTimeOffHours"] == 40.0
    assert tor["comments"][0]["textValue"] == "Beach week"


# ═══════════════════════════════════════════════════════════════════════════
# list_jobs
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_list_jobs(authed):
    respx.get(f"{BASE}/hr/v2/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [{"itemID": "j1"}]})
    )
    out = await authed.list_jobs(top=5)
    assert out["jobs"][0]["itemID"] == "j1"


# ═══════════════════════════════════════════════════════════════════════════
# refresh-on-401 — first call returns 401, client re-mints, second call 200
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_refresh_on_401(connector):
    # token mint endpoint (called twice: initial mint + refresh after 401)
    respx.post(TOKEN_URL).mock(
        side_effect=[
            _token_response(),
            httpx.Response(
                200,
                json={"access_token": "tok-fresh", "expires_in": 3600, "token_type": "Bearer"},
            ),
        ]
    )
    # workers: first 401, then 200
    respx.get(f"{BASE}/hr/v2/workers").mock(
        side_effect=[
            httpx.Response(401, json={"error": "expired"}),
            httpx.Response(200, json={"workers": [SAMPLE_WORKER]}),
        ]
    )
    out = await connector.list_workers(top=1)
    assert out["workers"][0]["associateOID"] == "G3XYZ123"
    # ensure cached token is the new one
    assert connector.http_client._access_token == "tok-fresh"


# ═══════════════════════════════════════════════════════════════════════════
# retry-on-429 — first 429, second 200
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@respx.mock
async def test_retry_on_429(authed, mocker):
    # short-circuit asyncio.sleep
    mocker.patch("client.http_client.asyncio.sleep", new=_no_sleep)
    respx.get(f"{BASE}/hr/v2/employees").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"}),
            httpx.Response(200, json={"employees": [{"associateOID": "E1"}]}),
        ]
    )
    out = await authed.list_employees(top=1)
    assert out["employees"][0]["associateOID"] == "E1"


async def _no_sleep(_):
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Identity / multi-tenant
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type():
    assert ADPConnector.CONNECTOR_TYPE == "adp"


def test_auth_type():
    assert ADPConnector.AUTH_TYPE == "oauth2"


def test_required_config_keys_defined():
    assert hasattr(ADPConnector, "REQUIRED_CONFIG_KEYS")
    for k in ("client_id", "client_secret", "cert_path", "key_path"):
        assert k in ADPConnector.REQUIRED_CONFIG_KEYS


@pytest.mark.asyncio
async def test_different_tenants_independent_instances():
    c1 = ADPConnector(tenant_id="tenant-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = ADPConnector(tenant_id="tenant-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.http_client is not c2.http_client
