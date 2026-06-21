"""Unit tests for AdpConnector — respx-mocked, zero real network I/O."""
import json as _json

import httpx
import pytest
import respx

from shared.base_connector import AuthStatus, ConnectorHealth

from connector import AdpConnector
from exceptions import (
    ADPAuthError,
    ADPBadRequestError,
    ADPConflictError,
    ADPNotFound,
    ADPNotFoundError,
    ADPRateLimitError,
)

from tests.conftest import (
    ADP_BASE,
    CONNECTOR_ID,
    SAMPLE_PAY_STATEMENT,
    SAMPLE_WORKER,
    TENANT_ID,
    TEST_CONFIG,
    TOKEN_URL,
    _token_response_json,
)


def _token_response() -> httpx.Response:
    return httpx.Response(200, json=_token_response_json())


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
async def test_install_missing_client_secret(connector):
    connector.config.pop("client_secret", None)
    result = await connector.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert result.health == ConnectorHealth.OFFLINE


@pytest.mark.asyncio
async def test_install_missing_client_cert(monkeypatch):
    cfg = dict(TEST_CONFIG)
    cfg.pop("client_cert")
    c = AdpConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_cert" in result.message


@pytest.mark.asyncio
async def test_install_missing_client_key(monkeypatch):
    cfg = dict(TEST_CONFIG)
    cfg.pop("client_key")
    c = AdpConnector(tenant_id=TENANT_ID, connector_id=CONNECTOR_ID, config=cfg)
    result = await c.install()
    assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
    assert "client_key" in result.message


# ═══════════════════════════════════════════════════════════════════════════
# authenticate() — verifies OAuth2 client-credentials body
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
# Bearer-header shape on resource calls
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_authorization_header_is_bearer_token(authed):
    route = respx.get(f"{ADP_BASE}/hr/v2/workers").mock(
        return_value=httpx.Response(200, json={"workers": []})
    )
    await authed.list_workers(top=1)
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == "Bearer test-access-token"
    assert route.calls[0].request.headers.get("accept") == "application/json"


# ═══════════════════════════════════════════════════════════════════════════
# health_check()
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_health_check_healthy(authed):
    respx.get(f"{ADP_BASE}/hr/v2/workers").mock(
        return_value=httpx.Response(200, json={"workers": [SAMPLE_WORKER]})
    )
    result = await authed.health_check()
    assert result.health == ConnectorHealth.HEALTHY
    assert result.auth_status == AuthStatus.CONNECTED


@respx.mock
@pytest.mark.asyncio
async def test_health_check_403_invalid_credentials(authed):
    respx.get(f"{ADP_BASE}/hr/v2/workers").mock(
        return_value=httpx.Response(403, json={"error": "forbidden"})
    )
    result = await authed.health_check()
    assert result.health == ConnectorHealth.UNHEALTHY
    assert result.auth_status == AuthStatus.INVALID_CREDENTIALS


# ═══════════════════════════════════════════════════════════════════════════
# list_workers — $filter + $top forwarded
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_workers_forwards_filter_and_top(authed):
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"workers": [SAMPLE_WORKER]})

    respx.get(f"{ADP_BASE}/hr/v2/workers").mock(side_effect=_handler)
    out = await authed.list_workers(
        top=25, filter="workerStatus/statusCode/codeValue eq 'Active'"
    )
    assert out["workers"][0]["associateOID"] == "G3XYZ123"
    assert captured["params"]["$top"] == "25"
    assert (
        captured["params"]["$filter"]
        == "workerStatus/statusCode/codeValue eq 'Active'"
    )


# ═══════════════════════════════════════════════════════════════════════════
# get_worker / list_employees
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_get_worker(authed):
    respx.get(f"{ADP_BASE}/hr/v2/workers/G3XYZ123").mock(
        return_value=httpx.Response(200, json={"worker": SAMPLE_WORKER})
    )
    out = await authed.get_worker("G3XYZ123")
    assert out["worker"]["associateOID"] == "G3XYZ123"


@respx.mock
@pytest.mark.asyncio
async def test_get_worker_404(authed):
    respx.get(f"{ADP_BASE}/hr/v2/workers/MISSING").mock(
        return_value=httpx.Response(404, json={"response": {"requestStatus": "NOT_FOUND"}})
    )
    with pytest.raises(ADPNotFound):
        await authed.get_worker("MISSING")


@respx.mock
@pytest.mark.asyncio
async def test_list_employees(authed):
    respx.get(f"{ADP_BASE}/hr/v2/employees").mock(
        return_value=httpx.Response(200, json={"employees": []})
    )
    out = await authed.list_employees(top=10)
    assert out == {"employees": []}


# ═══════════════════════════════════════════════════════════════════════════
# Payroll
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_pay_distributions(authed):
    respx.get(
        f"{ADP_BASE}/payroll/v1/workers/G3XYZ123/pay-distributions"
    ).mock(return_value=httpx.Response(200, json={"payDistributions": []}))
    out = await authed.list_pay_distributions("G3XYZ123")
    assert out == {"payDistributions": []}


@respx.mock
@pytest.mark.asyncio
async def test_list_payments(authed):
    respx.get(
        f"{ADP_BASE}/payroll/v1/workers/G3XYZ123/pay-statements"
    ).mock(return_value=httpx.Response(200, json={"payStatements": [SAMPLE_PAY_STATEMENT]}))
    out = await authed.list_payments("G3XYZ123", top=5)
    assert out["payStatements"][0]["payStatementID"] == "PS123"


@respx.mock
@pytest.mark.asyncio
async def test_get_payment_outputs(authed):
    respx.get(
        f"{ADP_BASE}/payroll/v1/workers/G3XYZ123/pay-statements/PS123"
    ).mock(return_value=httpx.Response(200, json={"payStatement": SAMPLE_PAY_STATEMENT}))
    out = await authed.get_payment_outputs("G3XYZ123", "PS123")
    assert out["payStatement"]["payStatementID"] == "PS123"


# ═══════════════════════════════════════════════════════════════════════════
# Time
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_time_cards(authed):
    respx.get(
        f"{ADP_BASE}/time/v2/workers/G3XYZ123/time-cards"
    ).mock(return_value=httpx.Response(200, json={"timeCards": []}))
    out = await authed.list_time_cards("G3XYZ123", top=10)
    assert out == {"timeCards": []}


@respx.mock
@pytest.mark.asyncio
async def test_submit_time_off_request_envelope(authed):
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.content.decode())
        return httpx.Response(200, json={"confirmMessage": {"requestStatus": "SUBMITTED"}})

    respx.post(
        f"{ADP_BASE}/time-off/v2/workers/G3XYZ123/time-off-requests"
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
# Benefits
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_benefits(authed):
    respx.get(
        f"{ADP_BASE}/benefits/v1/workers/G3XYZ123/enrollments"
    ).mock(return_value=httpx.Response(200, json={"enrollments": []}))
    out = await authed.list_benefits("G3XYZ123", top=10)
    assert out == {"enrollments": []}


# ═══════════════════════════════════════════════════════════════════════════
# Talent
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_list_jobs(authed):
    respx.get(f"{ADP_BASE}/hr/v2/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [{"itemID": "j1"}]})
    )
    out = await authed.list_jobs(top=5)
    assert out["jobs"][0]["itemID"] == "j1"


@respx.mock
@pytest.mark.asyncio
async def test_list_organizational_units(authed):
    respx.get(f"{ADP_BASE}/core/v1/organization-units").mock(
        return_value=httpx.Response(200, json={"organizationUnits": []})
    )
    out = await authed.list_organizational_units(top=5)
    assert out == {"organizationUnits": []}


# ═══════════════════════════════════════════════════════════════════════════
# Bad-request + conflict + rate-limit error mapping
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_bad_request_raises(authed):
    respx.get(f"{ADP_BASE}/hr/v2/workers").mock(
        return_value=httpx.Response(400, json={"error": "Invalid $filter"})
    )
    with pytest.raises(ADPBadRequestError):
        await authed.list_workers()


@respx.mock
@pytest.mark.asyncio
async def test_conflict_raises(authed):
    respx.post(
        f"{ADP_BASE}/time-off/v2/workers/G3XYZ123/time-off-requests"
    ).mock(return_value=httpx.Response(409, json={"error": "duplicate"}))
    with pytest.raises(ADPConflictError):
        await authed.submit_time_off_request(
            worker_aoid="G3XYZ123",
            policy_code="VAC",
            start_date="2026-07-01",
            end_date="2026-07-05",
        )


@respx.mock
@pytest.mark.asyncio
async def test_rate_limit_after_exhausting_retries(authed, no_retry_sleep):
    """All retries return 429 → final raise as ADPRateLimitError."""
    respx.get(f"{ADP_BASE}/hr/v2/employees").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"})
    )
    with pytest.raises(ADPRateLimitError):
        await authed.list_employees(top=1)


# ═══════════════════════════════════════════════════════════════════════════
# refresh-on-401 — first call returns 401, client re-mints, second call 200
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_refresh_on_401(connector):
    # token mint endpoint called twice: initial mint + refresh after 401
    respx.post(TOKEN_URL).mock(
        side_effect=[
            _token_response(),
            httpx.Response(
                200,
                json={"access_token": "tok-fresh", "expires_in": 3600, "token_type": "Bearer"},
            ),
        ]
    )
    respx.get(f"{ADP_BASE}/hr/v2/workers").mock(
        side_effect=[
            httpx.Response(401, json={"error": "expired"}),
            httpx.Response(200, json={"workers": [SAMPLE_WORKER]}),
        ]
    )
    out = await connector.list_workers(top=1)
    assert out["workers"][0]["associateOID"] == "G3XYZ123"
    assert connector.http_client._access_token == "tok-fresh"


# ═══════════════════════════════════════════════════════════════════════════
# retry-on-429 then success
# ═══════════════════════════════════════════════════════════════════════════

@respx.mock
@pytest.mark.asyncio
async def test_retry_on_429_then_success(authed, no_retry_sleep):
    respx.get(f"{ADP_BASE}/hr/v2/employees").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"}),
            httpx.Response(200, json={"employees": [{"associateOID": "E1"}]}),
        ]
    )
    out = await authed.list_employees(top=1)
    assert out["employees"][0]["associateOID"] == "E1"


@respx.mock
@pytest.mark.asyncio
async def test_retry_on_500_then_success(authed, no_retry_sleep):
    respx.get(f"{ADP_BASE}/hr/v2/jobs").mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json={"jobs": []}),
        ]
    )
    out = await authed.list_jobs(top=5)
    assert out == {"jobs": []}


# ═══════════════════════════════════════════════════════════════════════════
# Normalizer — id = f"{tenant_id}_{source_id}"
# ═══════════════════════════════════════════════════════════════════════════

def test_normalize_worker_id_shape():
    from helpers.normalizer import normalize_worker

    doc = normalize_worker(SAMPLE_WORKER, "conn-1", "tenant-X")
    assert doc.id == "tenant-X_G3XYZ123"
    assert doc.source_id == "G3XYZ123"
    assert doc.title == "Ada Lovelace"
    assert doc.metadata["kind"] == "adp.worker"
    assert doc.metadata["job_title"] == "Software Engineer"


def test_normalize_pay_statement_id_shape():
    from helpers.normalizer import normalize_pay_statement

    doc = normalize_pay_statement(SAMPLE_PAY_STATEMENT, "conn-1", "tenant-X")
    assert doc.id == "tenant-X_PS123"
    assert doc.metadata["kind"] == "adp.pay_statement"
    assert doc.metadata["net_pay"] == 4321.0


def test_normalize_time_off_request_id_shape():
    from helpers.normalizer import normalize_time_off_request

    raw = {
        "timeOffRequestID": "TOR-7",
        "startDate": "2026-07-01",
        "endDate": "2026-07-05",
        "timeOffPolicyCode": {"codeValue": "VAC"},
        "requestStatusCode": {"codeValue": "APPROVED"},
        "totalTimeOffHours": 40.0,
    }
    doc = normalize_time_off_request(raw, "conn-1", "tenant-X")
    assert doc.id == "tenant-X_TOR-7"
    assert doc.metadata["policy_code"] == "VAC"
    assert doc.metadata["status"] == "APPROVED"


# ═══════════════════════════════════════════════════════════════════════════
# Connector identity
# ═══════════════════════════════════════════════════════════════════════════

def test_connector_type_class_attr():
    assert AdpConnector.CONNECTOR_TYPE == "adp"


def test_auth_type_class_attr():
    assert AdpConnector.AUTH_TYPE == "oauth2_client_credentials"


def test_required_config_keys_defined():
    assert hasattr(AdpConnector, "REQUIRED_CONFIG_KEYS")
    for k in ("client_id", "client_secret", "client_cert", "client_key"):
        assert k in AdpConnector.REQUIRED_CONFIG_KEYS


def test_status_map_present():
    assert hasattr(AdpConnector, "_STATUS_MAP")
    assert 401 in AdpConnector._STATUS_MAP
    assert 403 in AdpConnector._STATUS_MAP
    assert 429 in AdpConnector._STATUS_MAP


# ═══════════════════════════════════════════════════════════════════════════
# Multi-tenant isolation
# ═══════════════════════════════════════════════════════════════════════════

def test_independent_instances_per_tenant():
    c1 = AdpConnector(tenant_id="t-A", connector_id="conn-1", config=dict(TEST_CONFIG))
    c2 = AdpConnector(tenant_id="t-B", connector_id="conn-2", config=dict(TEST_CONFIG))
    assert c1.tenant_id != c2.tenant_id
    assert c1.connector_id != c2.connector_id
    assert c1.http_client is not c2.http_client


# ═══════════════════════════════════════════════════════════════════════════
# PEM materialization
# ═══════════════════════════════════════════════════════════════════════════

def test_install_with_existing_cert_path(tmp_path):
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_text("-----BEGIN CERTIFICATE-----\nXYZ\n-----END CERTIFICATE-----\n")
    key.write_text("-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----\n")
    cfg = dict(TEST_CONFIG)
    cfg.pop("client_cert", None)
    cfg.pop("client_key", None)
    cfg["cert_path"] = str(cert)
    cfg["key_path"] = str(key)
    c = AdpConnector(tenant_id="t1", connector_id="c1", config=cfg)
    assert c.cert_path == str(cert)
    assert c.key_path == str(key)


# ═══════════════════════════════════════════════════════════════════════════
# Back-compat: legacy ADPConnector alias still works
# ═══════════════════════════════════════════════════════════════════════════

def test_legacy_alias_exported():
    from connector import ADPConnector
    assert ADPConnector is AdpConnector
