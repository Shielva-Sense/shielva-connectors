"""Unit tests for ConvertKitConnector — all HTTP calls are mocked."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import ConvertKitConnector
from exceptions import (
    ConvertKitAuthError,
    ConvertKitError,
    ConvertKitNetworkError,
    ConvertKitNotFoundError,
    ConvertKitRateLimitError,
    ConvertKitServerError,
)
from helpers.utils import (
    CircuitBreaker,
    _stable_id,
    normalize_form,
    normalize_sequence,
    normalize_subscriber,
    with_retry,
)
from models import AuthStatus, ConnectorHealth, ConnectorDocument, SyncStatus

TENANT_ID = "tenant_ck_001"
CONNECTOR_ID = "conn_convertkit_001"
VALID_API_KEY = "ck_abc123def456"
VALID_API_SECRET = "ck_secret_xyz789"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_ACCOUNT = {
    "name": "Acme Creators",
    "email": "admin@acme.com",
    "plan": "creator",
}

SAMPLE_SUBSCRIBER = {
    "id": 100001,
    "first_name": "Alice",
    "email_address": "alice@example.com",
    "state": "active",
    "created_at": "2024-01-10T08:00:00.000Z",
    "fields": {"last_name": "Wonder", "company": "ACME"},
}

SAMPLE_SUBSCRIBERS_PAGE_1 = {
    "total_subscribers": 2,
    "page": 1,
    "subscribers": [SAMPLE_SUBSCRIBER],
}

SAMPLE_SUBSCRIBER_2 = {
    "id": 100002,
    "first_name": "Bob",
    "email_address": "bob@example.com",
    "state": "inactive",
    "created_at": "2024-02-20T09:00:00.000Z",
    "fields": {},
}

SAMPLE_SUBSCRIBERS_PAGE_2 = {
    "total_subscribers": 2,
    "page": 2,
    "subscribers": [SAMPLE_SUBSCRIBER_2],
}

SAMPLE_SUBSCRIBERS_EMPTY = {
    "total_subscribers": 0,
    "page": 1,
    "subscribers": [],
}

SAMPLE_SEQUENCE = {
    "id": 200001,
    "name": "Welcome Series",
    "hold": False,
    "repeat": False,
    "created_at": "2024-01-15T10:00:00.000Z",
}

SAMPLE_SEQUENCES_RESPONSE = {
    "courses": [SAMPLE_SEQUENCE],
}

SAMPLE_FORM = {
    "id": 300001,
    "name": "Newsletter Signup",
    "type": "embed",
    "url": "https://acme.ck.page/newsletter",
    "embed_url": "https://api.convertkit.com/v3/forms/300001/subscribe",
    "created_at": "2024-01-20T11:00:00.000Z",
}

SAMPLE_FORMS_RESPONSE = {
    "forms": [SAMPLE_FORM],
}

SAMPLE_TAG = {
    "id": 400001,
    "name": "VIP",
    "created_at": "2024-01-05T07:00:00.000Z",
}

SAMPLE_TAGS_RESPONSE = {
    "tags": [SAMPLE_TAG],
}

SAMPLE_BROADCAST = {
    "id": 500001,
    "subject": "Monthly Newsletter",
    "created_at": "2024-06-01T12:00:00.000Z",
}

SAMPLE_BROADCASTS_RESPONSE = {
    "broadcasts": [SAMPLE_BROADCAST],
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_connector(api_key: str = VALID_API_KEY, api_secret: str = VALID_API_SECRET) -> ConvertKitConnector:
    return ConvertKitConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={"api_key": api_key, "api_secret": api_secret},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Exception hierarchy
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_base_error_attributes(self) -> None:
        err = ConvertKitError("base error", status_code=500, code="server_error")
        assert str(err) == "base error"
        assert err.status_code == 500
        assert err.code == "server_error"

    def test_auth_error_is_subclass(self) -> None:
        err = ConvertKitAuthError("auth fail", 401)
        assert isinstance(err, ConvertKitError)
        assert err.status_code == 401

    def test_network_error_no_status(self) -> None:
        err = ConvertKitNetworkError("timeout")
        assert isinstance(err, ConvertKitError)
        assert err.status_code == 0

    def test_not_found_error_message(self) -> None:
        err = ConvertKitNotFoundError("subscriber", "99999")
        assert "subscriber" in str(err)
        assert "99999" in str(err)
        assert err.status_code == 404
        assert err.code == "resource_missing"

    def test_rate_limit_error_retry_after(self) -> None:
        err = ConvertKitRateLimitError("rate limited", retry_after=30.0)
        assert err.status_code == 429
        assert err.retry_after == 30.0
        assert err.code == "rate_limit"

    def test_server_error_subclass(self) -> None:
        err = ConvertKitServerError("server down", 503)
        assert isinstance(err, ConvertKitError)
        assert err.status_code == 503


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Models
# ═══════════════════════════════════════════════════════════════════════════════

class TestModels:
    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="content",
            connector_id="conn1",
            tenant_id="t1",
        )
        assert doc.source_url == ""
        assert doc.metadata == {}

    def test_connector_document_with_metadata(self) -> None:
        doc = ConnectorDocument(
            source_id="abc",
            title="T",
            content="C",
            connector_id="c",
            tenant_id="t",
            source_url="https://example.com",
            metadata={"key": "val"},
        )
        assert doc.source_url == "https://example.com"
        assert doc.metadata["key"] == "val"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Normalizers
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizers:
    def test_stable_id_subscriber(self) -> None:
        doc_id = _stable_id("subscriber:", "100001")
        expected = hashlib.sha256(b"subscriber:100001").hexdigest()[:16]
        assert doc_id == expected
        assert len(doc_id) == 16

    def test_stable_id_sequence(self) -> None:
        doc_id = _stable_id("sequence:", "200001")
        expected = hashlib.sha256(b"sequence:200001").hexdigest()[:16]
        assert doc_id == expected

    def test_stable_id_form(self) -> None:
        doc_id = _stable_id("form:", "300001")
        expected = hashlib.sha256(b"form:300001").hexdigest()[:16]
        assert doc_id == expected

    def test_normalize_subscriber_full(self) -> None:
        doc = normalize_subscriber(SAMPLE_SUBSCRIBER, CONNECTOR_ID, TENANT_ID)
        assert "Alice" in doc.title
        assert "alice@example.com" in doc.title
        assert doc.metadata["type"] == "subscriber"
        assert doc.metadata["subscriber_id"] == 100001
        assert doc.metadata["email"] == "alice@example.com"
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID
        assert "subscriber" in doc.source_url
        assert "Alice" in doc.content
        assert "active" in doc.content

    def test_normalize_subscriber_no_first_name(self) -> None:
        sub = {"id": 999, "email_address": "nofirst@example.com", "state": "active"}
        doc = normalize_subscriber(sub)
        assert "nofirst@example.com" in doc.title
        assert doc.source_id == _stable_id("subscriber:", 999)

    def test_normalize_subscriber_with_fields(self) -> None:
        doc = normalize_subscriber(SAMPLE_SUBSCRIBER, CONNECTOR_ID, TENANT_ID)
        # custom fields present
        assert "ACME" in doc.content or doc.metadata["fields"].get("company") == "ACME"

    def test_normalize_sequence_full(self) -> None:
        doc = normalize_sequence(SAMPLE_SEQUENCE, CONNECTOR_ID, TENANT_ID)
        assert "Welcome Series" in doc.title
        assert doc.metadata["type"] == "sequence"
        assert doc.metadata["sequence_id"] == 200001
        assert doc.metadata["hold"] is False
        assert "sequence" in doc.source_url
        assert "Welcome Series" in doc.content

    def test_normalize_sequence_no_connector(self) -> None:
        doc = normalize_sequence(SAMPLE_SEQUENCE)
        assert doc.connector_id == ""
        assert doc.tenant_id == ""

    def test_normalize_form_full(self) -> None:
        doc = normalize_form(SAMPLE_FORM, CONNECTOR_ID, TENANT_ID)
        assert "Newsletter Signup" in doc.title
        assert doc.metadata["type"] == "form"
        assert doc.metadata["form_id"] == 300001
        assert doc.metadata["form_type"] == "embed"
        assert "Newsletter Signup" in doc.content
        assert doc.source_url == "https://acme.ck.page/newsletter"

    def test_normalize_form_no_url(self) -> None:
        form = {"id": 999, "name": "Empty Form"}
        doc = normalize_form(form)
        assert "Empty Form" in doc.title
        assert "999" in doc.source_url


# ═══════════════════════════════════════════════════════════════════════════════
# 4. with_retry
# ═══════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    async def test_success_on_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        fn.assert_awaited_once()

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(side_effect=[
            ConvertKitNetworkError("timeout"),
            ConvertKitNetworkError("timeout"),
            {"ok": True},
        ])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        assert fn.await_count == 3

    async def test_no_retry_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=ConvertKitAuthError("forbidden", 401))
        with pytest.raises(ConvertKitAuthError):
            await with_retry(fn)
        fn.assert_awaited_once()

    async def test_raises_after_max_attempts(self) -> None:
        fn = AsyncMock(side_effect=ConvertKitNetworkError("down"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ConvertKitNetworkError):
                await with_retry(fn, max_attempts=2, base_delay=0)
        assert fn.await_count == 2

    async def test_rate_limit_uses_retry_after(self) -> None:
        fn = AsyncMock(side_effect=[
            ConvertKitRateLimitError("rate limited", retry_after=5.0),
            {"ok": True},
        ])
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=3, base_delay=0)
        assert result == {"ok": True}
        sleep_mock.assert_awaited_once_with(5.0)

    async def test_passes_args_and_kwargs(self) -> None:
        fn = AsyncMock(return_value={"result": 42})
        result = await with_retry(fn, "arg1", key="val")
        fn.assert_awaited_once_with("arg1", key="val")
        assert result == {"result": 42}


# ═══════════════════════════════════════════════════════════════════════════════
# 5. CircuitBreaker
# ═══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_initial_state_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        assert cb.state == "closed"
        assert not cb.is_open

    def test_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.on_failure()
        cb.on_failure()
        assert cb.state == "closed"
        cb.on_failure()
        assert cb.is_open

    def test_resets_on_success(self) -> None:
        cb = CircuitBreaker(failure_threshold=2)
        cb.on_failure()
        cb.on_failure()
        assert cb.is_open
        cb.on_success()
        assert not cb.is_open
        assert cb.state == "closed"

    def test_half_open_after_timeout(self) -> None:
        import time
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.01)
        cb.on_failure()
        assert cb.is_open
        time.sleep(0.05)
        assert cb.state == "half-open"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. HTTP client (ConvertKitHTTPClient)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHTTPClient:
    """Test HTTP client methods via mocked aiohttp session."""

    def _make_http_client(self, api_key: str = VALID_API_KEY, api_secret: str = VALID_API_SECRET):
        from client.http_client import ConvertKitHTTPClient
        return ConvertKitHTTPClient(api_key=api_key, api_secret=api_secret)

    def _mock_response(self, json_data: dict, status: int = 200):
        resp = MagicMock()
        resp.status = status
        resp.json = AsyncMock(return_value=json_data)
        resp.text = AsyncMock(return_value="")
        resp.headers = {}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=None)
        return resp

    async def test_get_account_api_key_in_params(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response(SAMPLE_ACCOUNT)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        result = await client.get_account()
        assert result == SAMPLE_ACCOUNT
        call_kwargs = mock_session.get.call_args
        params = call_kwargs[1].get("params", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {})
        assert params.get("api_key") == VALID_API_KEY

    async def test_get_subscribers_uses_api_secret(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response(SAMPLE_SUBSCRIBERS_PAGE_1)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        result = await client.get_subscribers(page=1)
        assert result == SAMPLE_SUBSCRIBERS_PAGE_1
        call_kwargs = mock_session.get.call_args
        params = call_kwargs[1].get("params", {})
        assert params.get("api_secret") == VALID_API_SECRET

    async def test_get_subscriber_by_id(self) -> None:
        client = self._make_http_client()
        single_response = {"subscriber": SAMPLE_SUBSCRIBER}
        mock_resp = self._mock_response(single_response)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        result = await client.get_subscriber(100001)
        assert result == single_response
        call_args = mock_session.get.call_args[0]
        assert "100001" in call_args[0]

    async def test_get_tags(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response(SAMPLE_TAGS_RESPONSE)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        result = await client.get_tags()
        assert result == SAMPLE_TAGS_RESPONSE

    async def test_get_sequences(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response(SAMPLE_SEQUENCES_RESPONSE)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        result = await client.get_sequences()
        assert result == SAMPLE_SEQUENCES_RESPONSE

    async def test_get_forms(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response(SAMPLE_FORMS_RESPONSE)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        result = await client.get_forms()
        assert result == SAMPLE_FORMS_RESPONSE

    async def test_get_broadcasts(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response(SAMPLE_BROADCASTS_RESPONSE)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        result = await client.get_broadcasts()
        assert result == SAMPLE_BROADCASTS_RESPONSE

    async def test_raise_for_status_401(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response({"message": "Unauthorized"}, status=401)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        with pytest.raises(ConvertKitAuthError):
            await client.get_account()

    async def test_raise_for_status_403(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response({"message": "Forbidden"}, status=403)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        with pytest.raises(ConvertKitAuthError):
            await client.get_account()

    async def test_raise_for_status_404(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response({}, status=404)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        with pytest.raises(ConvertKitNotFoundError):
            await client.get_subscriber(9999)

    async def test_raise_for_status_429(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response({"message": "Too many requests"}, status=429)
        mock_resp.headers = {"Retry-After": "10"}
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        with pytest.raises(ConvertKitRateLimitError) as exc_info:
            await client.get_account()
        assert exc_info.value.retry_after == 10.0

    async def test_raise_for_status_500(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response({"message": "Internal server error"}, status=500)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        with pytest.raises(ConvertKitServerError):
            await client.get_account()

    async def test_raise_for_status_other_4xx(self) -> None:
        client = self._make_http_client()
        mock_resp = self._mock_response({"message": "Bad request"}, status=400)
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=mock_resp)
        client._session = mock_session
        with pytest.raises(ConvertKitError):
            await client.get_account()

    async def test_aclose_closes_session(self) -> None:
        client = self._make_http_client()
        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        client._session = mock_session
        await client.aclose()
        mock_session.close.assert_awaited_once()
        assert client._session is None

    async def test_context_manager(self) -> None:
        from client.http_client import ConvertKitHTTPClient
        client = ConvertKitHTTPClient(api_key=VALID_API_KEY)
        async with client as c:
            assert c is client
        # session closed on exit — no error


# ═══════════════════════════════════════════════════════════════════════════════
# 7. install()
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    async def test_install_success(self) -> None:
        conn = make_connector()
        conn._make_client = MagicMock(return_value=MagicMock(
            get_account=AsyncMock(return_value=SAMPLE_ACCOUNT),
            aclose=AsyncMock(),
        ))
        result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Acme Creators" in result.message

    async def test_install_missing_api_key(self) -> None:
        conn = ConvertKitConnector(config={})
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message

    async def test_install_invalid_key(self) -> None:
        conn = make_connector()
        conn._make_client = MagicMock(return_value=MagicMock(
            get_account=AsyncMock(side_effect=ConvertKitAuthError("invalid", 401)),
            aclose=AsyncMock(),
        ))
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_network_error(self) -> None:
        conn = make_connector()
        conn._make_client = MagicMock(return_value=MagicMock(
            get_account=AsyncMock(side_effect=ConvertKitNetworkError("timeout")),
            aclose=AsyncMock(),
        ))
        result = await conn.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED

    async def test_install_stores_connector_id(self) -> None:
        conn = make_connector()
        conn._make_client = MagicMock(return_value=MagicMock(
            get_account=AsyncMock(return_value=SAMPLE_ACCOUNT),
            aclose=AsyncMock(),
        ))
        result = await conn.install()
        assert result.connector_id == CONNECTOR_ID

    async def test_install_account_with_email_only(self) -> None:
        conn = make_connector()
        conn._make_client = MagicMock(return_value=MagicMock(
            get_account=AsyncMock(return_value={"email": "user@example.com"}),
            aclose=AsyncMock(),
        ))
        result = await conn.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert "user@example.com" in result.message


# ═══════════════════════════════════════════════════════════════════════════════
# 8. health_check()
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    async def test_health_check_success(self) -> None:
        conn = make_connector()
        conn._make_client = MagicMock(return_value=MagicMock(
            get_account=AsyncMock(return_value=SAMPLE_ACCOUNT),
            aclose=AsyncMock(),
        ))
        result = await conn.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Acme Creators" in result.message

    async def test_health_check_missing_api_key(self) -> None:
        conn = ConvertKitConnector(config={})
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        conn = make_connector()
        conn._make_client = MagicMock(return_value=MagicMock(
            get_account=AsyncMock(side_effect=ConvertKitAuthError("revoked", 401)),
            aclose=AsyncMock(),
        ))
        result = await conn.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_network_error_degraded(self) -> None:
        conn = make_connector()
        conn._make_client = MagicMock(return_value=MagicMock(
            get_account=AsyncMock(side_effect=ConvertKitNetworkError("timeout")),
            aclose=AsyncMock(),
        ))
        result = await conn.health_check()
        assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)
        assert result.auth_status == AuthStatus.FAILED

    async def test_health_check_circuit_breaker_success(self) -> None:
        conn = make_connector()
        conn._make_client = MagicMock(return_value=MagicMock(
            get_account=AsyncMock(return_value=SAMPLE_ACCOUNT),
            aclose=AsyncMock(),
        ))
        await conn.health_check()
        assert not conn._circuit_breaker.is_open

    async def test_health_check_generic_exception_degraded(self) -> None:
        conn = make_connector()
        conn._make_client = MagicMock(return_value=MagicMock(
            get_account=AsyncMock(side_effect=RuntimeError("unexpected")),
            aclose=AsyncMock(),
        ))
        result = await conn.health_check()
        assert result.health == ConnectorHealth.DEGRADED


# ═══════════════════════════════════════════════════════════════════════════════
# 9. sync()
# ═══════════════════════════════════════════════════════════════════════════════

class TestSync:
    def _patched_client(
        self,
        subscribers_response=None,
        sequences_response=None,
        forms_response=None,
    ) -> MagicMock:
        c = MagicMock()
        c.get_subscribers = AsyncMock(return_value=subscribers_response or SAMPLE_SUBSCRIBERS_PAGE_1)
        c.get_sequences = AsyncMock(return_value=sequences_response or SAMPLE_SEQUENCES_RESPONSE)
        c.get_forms = AsyncMock(return_value=forms_response or SAMPLE_FORMS_RESPONSE)
        c.aclose = AsyncMock()
        return c

    async def test_sync_success_counts(self) -> None:
        conn = make_connector()
        conn.http_client = self._patched_client()
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        # 1 subscriber + 1 sequence + 1 form = 3
        assert result.documents_found == 3
        assert result.documents_synced == 3
        assert result.documents_failed == 0

    async def test_sync_empty_subscribers(self) -> None:
        conn = make_connector()
        conn.http_client = self._patched_client(
            subscribers_response=SAMPLE_SUBSCRIBERS_EMPTY
        )
        result = await conn.sync()
        assert result.status == SyncStatus.COMPLETED
        # 0 subscribers + 1 sequence + 1 form = 2
        assert result.documents_found == 2
        assert result.documents_synced == 2

    async def test_sync_subscriber_api_failure_returns_failed(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_subscribers = AsyncMock(
            side_effect=ConvertKitError("API error", 500)
        )
        conn.http_client = mock_client
        result = await conn.sync()
        assert result.status == SyncStatus.FAILED

    async def test_sync_sequences_failure_nonfatal(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_subscribers = AsyncMock(return_value=SAMPLE_SUBSCRIBERS_PAGE_1)
        mock_client.get_sequences = AsyncMock(side_effect=ConvertKitError("seq error"))
        mock_client.get_forms = AsyncMock(return_value=SAMPLE_FORMS_RESPONSE)
        conn.http_client = mock_client
        result = await conn.sync()
        # subscriber + form synced; sequences skipped (non-fatal)
        assert result.documents_synced >= 1

    async def test_sync_forms_failure_nonfatal(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_subscribers = AsyncMock(return_value=SAMPLE_SUBSCRIBERS_PAGE_1)
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_SEQUENCES_RESPONSE)
        mock_client.get_forms = AsyncMock(side_effect=ConvertKitError("form error"))
        conn.http_client = mock_client
        result = await conn.sync()
        # subscriber + sequence synced; forms skipped
        assert result.documents_synced == 2

    async def test_sync_with_kb_id_calls_ingest(self) -> None:
        conn = make_connector()
        conn.http_client = self._patched_client()
        conn._ingest_document = AsyncMock()
        result = await conn.sync(kb_id="kb_001")
        assert conn._ingest_document.await_count >= 1
        assert result.documents_synced > 0

    async def test_sync_partial_on_normalizer_failure(self) -> None:
        conn = make_connector()
        # subscriber with bad data that triggers normalizer exception
        bad_sub = {"id": None, "email_address": None, "state": None}
        conn.http_client = MagicMock()
        conn.http_client.get_subscribers = AsyncMock(return_value={
            "total_subscribers": 1,
            "page": 1,
            "subscribers": [bad_sub],
        })
        conn.http_client.get_sequences = AsyncMock(return_value=SAMPLE_SEQUENCES_RESPONSE)
        conn.http_client.get_forms = AsyncMock(return_value=SAMPLE_FORMS_RESPONSE)

        # Patch normalize_subscriber to raise
        with patch("connector.normalize_subscriber", side_effect=ValueError("bad")):
            result = await conn.sync()
        assert result.documents_failed >= 1
        assert result.status == SyncStatus.PARTIAL

    async def test_sync_initialises_client_if_none(self) -> None:
        conn = make_connector()
        assert conn.http_client is None
        mock_client = self._patched_client()
        conn._make_client = MagicMock(return_value=mock_client)
        result = await conn.sync()
        assert conn.http_client is not None
        assert result.status == SyncStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# 10. List methods
# ═══════════════════════════════════════════════════════════════════════════════

class TestListMethods:
    def _make_conn_with_mock_client(self) -> tuple[ConvertKitConnector, MagicMock]:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.get_subscribers = AsyncMock(return_value=SAMPLE_SUBSCRIBERS_PAGE_1)
        mock_client.get_subscriber = AsyncMock(return_value={"subscriber": SAMPLE_SUBSCRIBER})
        mock_client.get_tags = AsyncMock(return_value=SAMPLE_TAGS_RESPONSE)
        mock_client.get_sequences = AsyncMock(return_value=SAMPLE_SEQUENCES_RESPONSE)
        mock_client.get_forms = AsyncMock(return_value=SAMPLE_FORMS_RESPONSE)
        mock_client.get_broadcasts = AsyncMock(return_value=SAMPLE_BROADCASTS_RESPONSE)
        mock_client.aclose = AsyncMock()
        conn.http_client = mock_client
        return conn, mock_client

    async def test_list_subscribers_returns_data(self) -> None:
        conn, mc = self._make_conn_with_mock_client()
        result = await conn.list_subscribers()
        assert result == SAMPLE_SUBSCRIBERS_PAGE_1
        mc.get_subscribers.assert_awaited_once()

    async def test_list_subscribers_passes_page(self) -> None:
        conn, mc = self._make_conn_with_mock_client()
        await conn.list_subscribers(page=2, per_page=50)
        mc.get_subscribers.assert_awaited_once_with(page=2, per_page=50)

    async def test_list_subscribers_empty(self) -> None:
        conn = make_connector()
        conn.http_client = MagicMock()
        conn.http_client.get_subscribers = AsyncMock(return_value=SAMPLE_SUBSCRIBERS_EMPTY)
        result = await conn.list_subscribers()
        assert result["subscribers"] == []
        assert result["total_subscribers"] == 0

    async def test_get_subscriber_returns_data(self) -> None:
        conn, mc = self._make_conn_with_mock_client()
        result = await conn.get_subscriber(100001)
        assert result == {"subscriber": SAMPLE_SUBSCRIBER}
        mc.get_subscriber.assert_awaited_once_with(100001)

    async def test_list_tags_returns_data(self) -> None:
        conn, mc = self._make_conn_with_mock_client()
        result = await conn.list_tags()
        assert result == SAMPLE_TAGS_RESPONSE
        mc.get_tags.assert_awaited_once()

    async def test_list_tags_page_param(self) -> None:
        conn, mc = self._make_conn_with_mock_client()
        await conn.list_tags(page=3)
        mc.get_tags.assert_awaited_once_with(page=3)

    async def test_list_sequences_returns_data(self) -> None:
        conn, mc = self._make_conn_with_mock_client()
        result = await conn.list_sequences()
        assert result == SAMPLE_SEQUENCES_RESPONSE

    async def test_list_sequences_page_param(self) -> None:
        conn, mc = self._make_conn_with_mock_client()
        await conn.list_sequences(page=2)
        mc.get_sequences.assert_awaited_once_with(page=2)

    async def test_list_forms_returns_data(self) -> None:
        conn, mc = self._make_conn_with_mock_client()
        result = await conn.list_forms()
        assert result == SAMPLE_FORMS_RESPONSE

    async def test_list_forms_empty(self) -> None:
        conn = make_connector()
        conn.http_client = MagicMock()
        conn.http_client.get_forms = AsyncMock(return_value={"forms": []})
        result = await conn.list_forms()
        assert result["forms"] == []

    async def test_list_broadcasts_returns_data(self) -> None:
        conn, mc = self._make_conn_with_mock_client()
        result = await conn.list_broadcasts()
        assert result == SAMPLE_BROADCASTS_RESPONSE

    async def test_list_broadcasts_page_param(self) -> None:
        conn, mc = self._make_conn_with_mock_client()
        await conn.list_broadcasts(page=5)
        mc.get_broadcasts.assert_awaited_once_with(page=5)


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Connector lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorLifecycle:
    def test_constructor_defaults(self) -> None:
        conn = ConvertKitConnector()
        assert conn._api_key == ""
        assert conn._api_secret == ""
        assert conn.http_client is None

    def test_constructor_with_config(self) -> None:
        conn = make_connector()
        assert conn._api_key == VALID_API_KEY
        assert conn._api_secret == VALID_API_SECRET
        assert conn.tenant_id == TENANT_ID
        assert conn.connector_id == CONNECTOR_ID

    async def test_aclose_closes_http_client(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        conn.http_client = mock_client
        await conn.aclose()
        mock_client.aclose.assert_awaited_once()
        assert conn.http_client is None

    async def test_aclose_noop_when_no_client(self) -> None:
        conn = make_connector()
        # Should not raise
        await conn.aclose()

    async def test_context_manager(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        conn._make_client = MagicMock(return_value=mock_client)
        async with conn as c:
            assert c is conn
        mock_client.aclose.assert_not_awaited()  # client only set if used

    def test_connector_type_and_auth_type(self) -> None:
        conn = make_connector()
        assert conn.CONNECTOR_TYPE == "convertkit"
        assert conn.AUTH_TYPE == "api_key"

    async def test_ensure_client_creates_on_first_call(self) -> None:
        conn = make_connector()
        assert conn.http_client is None
        client = conn._ensure_client()
        assert conn.http_client is not None
        assert client is conn.http_client

    async def test_ensure_client_reuses_existing(self) -> None:
        conn = make_connector()
        mock_client = MagicMock()
        conn.http_client = mock_client
        client = conn._ensure_client()
        assert client is mock_client
