"""Unit tests for ClearbitConnector — all HTTP calls are mocked."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import ClearbitConnector
from exceptions import (
    ClearbitAuthError,
    ClearbitError,
    ClearbitNetworkError,
    ClearbitNotFoundError,
    ClearbitRateLimitError,
)
from helpers.utils import (
    _stable_id,
    normalize_combined,
    normalize_company,
    normalize_person,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    SyncStatus,
)

TENANT_ID = "tenant_clearbit_test"
CONNECTOR_ID = "conn_clearbit_test_001"
VALID_API_KEY = "sk-clearbit-testkey-abc123"

# ── Sample data ──────────────────────────────────────────────────────────────

SAMPLE_COMPANY: dict = {
    "id": "027b0d40-016c-40ea-8925-a076fa640992",
    "name": "Clearbit",
    "legalName": "Clearbit, Inc.",
    "domain": "clearbit.com",
    "description": "Business intelligence for sales and marketing teams.",
    "foundedYear": 2012,
    "category": {
        "industry": "Software",
        "sector": "Technology",
    },
    "geo": {
        "city": "San Francisco",
        "country": "United States",
        "countryCode": "US",
    },
    "metrics": {
        "employees": 250,
        "estimatedAnnualRevenue": "$10M-$50M",
    },
    "linkedin": {"handle": "clearbit"},
}

SAMPLE_PERSON: dict = {
    "id": "person-abc123",
    "name": {
        "fullName": "Alex Johnson",
        "givenName": "Alex",
        "familyName": "Johnson",
    },
    "email": "alex@stripe.com",
    "location": "San Francisco, CA",
    "bio": "Engineer at Stripe building payment infrastructure.",
    "site": "https://alexjohnson.dev",
    "employment": {
        "name": "Stripe",
        "title": "Software Engineer",
        "role": "engineering",
    },
    "linkedin": {"handle": "alexjohnson"},
}

SAMPLE_COMBINED: dict = {
    "person": SAMPLE_PERSON,
    "company": SAMPLE_COMPANY,
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. EXCEPTION CLASSES
# ═══════════════════════════════════════════════════════════════════════════════


class TestExceptions:
    """All five exception classes behave correctly."""

    def test_clearbit_error_base(self) -> None:
        exc = ClearbitError("base error", status_code=500, code="server_error")
        assert str(exc) == "base error"
        assert exc.message == "base error"
        assert exc.status_code == 500
        assert exc.code == "server_error"

    def test_clearbit_error_defaults(self) -> None:
        exc = ClearbitError("minimal")
        assert exc.status_code == 0
        assert exc.code == ""

    def test_clearbit_auth_error(self) -> None:
        exc = ClearbitAuthError("Unauthorized", 401, "auth_failed")
        assert isinstance(exc, ClearbitError)
        assert exc.status_code == 401
        assert "Unauthorized" in str(exc)

    def test_clearbit_network_error(self) -> None:
        exc = ClearbitNetworkError("Connection refused")
        assert isinstance(exc, ClearbitError)
        assert "Connection refused" in str(exc)

    def test_clearbit_not_found_error(self) -> None:
        exc = ClearbitNotFoundError("company", "example.com")
        assert isinstance(exc, ClearbitError)
        assert exc.status_code == 404
        assert exc.code == "resource_missing"
        assert exc.resource == "company"
        assert exc.identifier == "example.com"
        assert "example.com" in str(exc)

    def test_clearbit_rate_limit_error(self) -> None:
        exc = ClearbitRateLimitError("Rate limited", retry_after=10.0)
        assert isinstance(exc, ClearbitError)
        assert exc.status_code == 429
        assert exc.code == "rate_limit"
        assert exc.retry_after == 10.0

    def test_clearbit_rate_limit_default_retry(self) -> None:
        exc = ClearbitRateLimitError("Rate limited")
        assert exc.retry_after == 0.0

    def test_exception_hierarchy(self) -> None:
        """All exceptions inherit from ClearbitError."""
        for cls in (
            ClearbitAuthError,
            ClearbitNetworkError,
            ClearbitNotFoundError,
            ClearbitRateLimitError,
        ):
            assert issubclass(cls, ClearbitError)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. MODELS
# ═══════════════════════════════════════════════════════════════════════════════


class TestModels:
    def test_connector_health_values(self) -> None:
        from models import ConnectorHealth
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
        from models import AuthStatus
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"
        assert SyncStatus.RUNNING == "running"

    def test_install_result_defaults(self) -> None:
        from models import InstallResult
        r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result_defaults(self) -> None:
        from models import HealthCheckResult
        r = HealthCheckResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.message == ""

    def test_sync_result_defaults(self) -> None:
        from models import SyncResult
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="Content",
            connector_id="conn1",
            tenant_id="t1",
        )
        assert doc.source_id == "abc123"
        assert doc.source_url == ""
        assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. NORMALIZERS
# ═══════════════════════════════════════════════════════════════════════════════


class TestNormalizeCompany:
    def test_stable_id_from_domain(self) -> None:
        doc = normalize_company(SAMPLE_COMPANY, "conn1", "t1")
        expected = _stable_id("company", "clearbit.com")
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_type_is_company(self) -> None:
        doc = normalize_company(SAMPLE_COMPANY)
        assert doc.metadata["type"] == "company"

    def test_name_in_metadata(self) -> None:
        doc = normalize_company(SAMPLE_COMPANY)
        assert doc.metadata["name"] == "Clearbit"

    def test_domain_in_metadata(self) -> None:
        doc = normalize_company(SAMPLE_COMPANY)
        assert doc.metadata["domain"] == "clearbit.com"

    def test_industry_in_metadata(self) -> None:
        doc = normalize_company(SAMPLE_COMPANY)
        assert doc.metadata["industry"] == "Software"

    def test_location_in_metadata(self) -> None:
        doc = normalize_company(SAMPLE_COMPANY)
        assert "San Francisco" in doc.metadata["location"]

    def test_title_contains_company_name(self) -> None:
        doc = normalize_company(SAMPLE_COMPANY)
        assert "Clearbit" in doc.title

    def test_content_has_domain(self) -> None:
        doc = normalize_company(SAMPLE_COMPANY)
        assert "clearbit.com" in doc.content

    def test_source_url_includes_domain(self) -> None:
        doc = normalize_company(SAMPLE_COMPANY)
        assert "clearbit.com" in doc.source_url

    def test_connector_id_and_tenant_propagated(self) -> None:
        doc = normalize_company(SAMPLE_COMPANY, "conn42", "tenant99")
        assert doc.connector_id == "conn42"
        assert doc.tenant_id == "tenant99"

    def test_empty_company_does_not_crash(self) -> None:
        doc = normalize_company({})
        assert isinstance(doc, ConnectorDocument)
        assert doc.metadata["type"] == "company"

    def test_stable_id_is_deterministic(self) -> None:
        doc1 = normalize_company(SAMPLE_COMPANY)
        doc2 = normalize_company(SAMPLE_COMPANY)
        assert doc1.source_id == doc2.source_id


class TestNormalizePerson:
    def test_stable_id_from_email(self) -> None:
        doc = normalize_person(SAMPLE_PERSON, "conn1", "t1")
        expected = _stable_id("person", "alex@stripe.com")
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_type_is_person(self) -> None:
        doc = normalize_person(SAMPLE_PERSON)
        assert doc.metadata["type"] == "person"

    def test_name_extracted(self) -> None:
        doc = normalize_person(SAMPLE_PERSON)
        assert doc.metadata["name"] == "Alex Johnson"

    def test_email_in_metadata(self) -> None:
        doc = normalize_person(SAMPLE_PERSON)
        assert doc.metadata["email"] == "alex@stripe.com"

    def test_title_in_metadata(self) -> None:
        doc = normalize_person(SAMPLE_PERSON)
        assert doc.metadata["title"] == "Software Engineer"

    def test_company_in_metadata(self) -> None:
        doc = normalize_person(SAMPLE_PERSON)
        assert doc.metadata["company"] == "Stripe"

    def test_title_contains_person_name(self) -> None:
        doc = normalize_person(SAMPLE_PERSON)
        assert "Alex Johnson" in doc.title

    def test_content_has_email(self) -> None:
        doc = normalize_person(SAMPLE_PERSON)
        assert "alex@stripe.com" in doc.content

    def test_empty_person_does_not_crash(self) -> None:
        doc = normalize_person({})
        assert isinstance(doc, ConnectorDocument)
        assert doc.metadata["type"] == "person"

    def test_stable_id_is_deterministic(self) -> None:
        doc1 = normalize_person(SAMPLE_PERSON)
        doc2 = normalize_person(SAMPLE_PERSON)
        assert doc1.source_id == doc2.source_id


class TestNormalizeCombined:
    def test_stable_id_from_email(self) -> None:
        doc = normalize_combined(SAMPLE_COMBINED, "conn1", "t1")
        expected = _stable_id("combined", "alex@stripe.com")
        assert doc.source_id == expected
        assert len(doc.source_id) == 16

    def test_type_is_combined(self) -> None:
        doc = normalize_combined(SAMPLE_COMBINED)
        assert doc.metadata["type"] == "combined"

    def test_person_name_extracted(self) -> None:
        doc = normalize_combined(SAMPLE_COMBINED)
        assert doc.metadata["person_name"] == "Alex Johnson"

    def test_company_name_extracted(self) -> None:
        doc = normalize_combined(SAMPLE_COMBINED)
        assert doc.metadata["company_name"] == "Clearbit"

    def test_company_domain_extracted(self) -> None:
        doc = normalize_combined(SAMPLE_COMBINED)
        assert doc.metadata["company_domain"] == "clearbit.com"

    def test_industry_from_company(self) -> None:
        doc = normalize_combined(SAMPLE_COMBINED)
        assert doc.metadata["industry"] == "Software"

    def test_title_contains_both(self) -> None:
        doc = normalize_combined(SAMPLE_COMBINED)
        assert "Alex Johnson" in doc.title
        assert "Clearbit" in doc.title

    def test_handles_partial_data_no_company(self) -> None:
        data = {"person": SAMPLE_PERSON, "company": None}
        doc = normalize_combined(data)
        assert isinstance(doc, ConnectorDocument)
        assert doc.metadata["type"] == "combined"

    def test_handles_empty_combined(self) -> None:
        doc = normalize_combined({})
        assert isinstance(doc, ConnectorDocument)

    def test_stable_id_is_deterministic(self) -> None:
        doc1 = normalize_combined(SAMPLE_COMBINED)
        doc2 = normalize_combined(SAMPLE_COMBINED)
        assert doc1.source_id == doc2.source_id


# ═══════════════════════════════════════════════════════════════════════════════
# 4. WITH_RETRY
# ═══════════════════════════════════════════════════════════════════════════════


class TestWithRetry:
    async def test_success_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        assert fn.call_count == 1

    async def test_retries_on_network_error(self) -> None:
        fn = AsyncMock(
            side_effect=[
                ClearbitNetworkError("timeout"),
                ClearbitNetworkError("timeout"),
                {"ok": True},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        assert fn.call_count == 3

    async def test_no_retry_on_auth_error(self) -> None:
        fn = AsyncMock(side_effect=ClearbitAuthError("Unauthorized", 401))
        with pytest.raises(ClearbitAuthError):
            await with_retry(fn, max_attempts=3)
        assert fn.call_count == 1

    async def test_exhausted_raises_last_exception(self) -> None:
        fn = AsyncMock(side_effect=ClearbitNetworkError("timeout"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ClearbitNetworkError):
                await with_retry(fn, max_attempts=3)
        assert fn.call_count == 3

    async def test_202_pending_is_retried(self) -> None:
        """A 202 Accepted (enrichment pending) becomes ClearbitNotFoundError and IS retried."""
        fn = AsyncMock(
            side_effect=[
                ClearbitNotFoundError("enrichment", "url"),
                {"name": "Clearbit", "domain": "clearbit.com"},
            ]
        )
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_attempts=3)
        assert result["domain"] == "clearbit.com"
        assert fn.call_count == 2

    async def test_rate_limit_retry_uses_retry_after(self) -> None:
        fn = AsyncMock(
            side_effect=[
                ClearbitRateLimitError("Rate limited", retry_after=5.0),
                {"ok": True},
            ]
        )
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_attempts=3)
        assert result == {"ok": True}
        sleep_mock.assert_called_once_with(5.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. HTTP CLIENT
# ═══════════════════════════════════════════════════════════════════════════════


class TestClearbitHTTPClient:
    """ClearbitHTTPClient — mocked session, all endpoints, error mapping."""

    def _make_mock_response(
        self,
        status: int,
        json_data: dict | list | None = None,
        text_data: str = "",
        headers: dict | None = None,
    ) -> MagicMock:
        resp = MagicMock()
        resp.status = status
        resp.headers = headers or {}
        resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
        resp.text = AsyncMock(return_value=text_data)
        resp.read = AsyncMock(return_value=b"")
        resp.content_length = 100
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    async def test_enrich_company_success(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        mock_resp = self._make_mock_response(200, SAMPLE_COMPANY)
        with patch.object(client, "_get_session") as mock_sess:
            mock_sess.return_value.request.return_value.__aenter__ = AsyncMock(
                return_value=mock_resp
            )
            mock_sess.return_value.request.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.enrich_company("clearbit.com")
        assert result["domain"] == "clearbit.com"
        await client.aclose()

    async def test_enrich_person_success(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        mock_resp = self._make_mock_response(200, SAMPLE_PERSON)
        with patch.object(client, "_get_session") as mock_sess:
            mock_sess.return_value.request.return_value.__aenter__ = AsyncMock(
                return_value=mock_resp
            )
            mock_sess.return_value.request.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.enrich_person("alex@stripe.com")
        assert result["email"] == "alex@stripe.com"
        await client.aclose()

    async def test_combined_lookup_success(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        mock_resp = self._make_mock_response(200, SAMPLE_COMBINED)
        with patch.object(client, "_get_session") as mock_sess:
            mock_sess.return_value.request.return_value.__aenter__ = AsyncMock(
                return_value=mock_resp
            )
            mock_sess.return_value.request.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.combined_lookup("alex@stripe.com")
        assert "person" in result
        assert "company" in result
        await client.aclose()

    async def test_search_companies_returns_list(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        suggestions = [{"name": "Stripe", "domain": "stripe.com"}]
        mock_resp = self._make_mock_response(200, suggestions)
        with patch.object(client, "_get_session") as mock_sess:
            mock_sess.return_value.request.return_value.__aenter__ = AsyncMock(
                return_value=mock_resp
            )
            mock_sess.return_value.request.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.search_companies("stripe")
        assert isinstance(result, list)
        assert result[0]["domain"] == "stripe.com"
        await client.aclose()

    async def test_reveal_ip_success(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        mock_resp = self._make_mock_response(200, SAMPLE_COMPANY)
        with patch.object(client, "_get_session") as mock_sess:
            mock_sess.return_value.request.return_value.__aenter__ = AsyncMock(
                return_value=mock_resp
            )
            mock_sess.return_value.request.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await client.reveal_ip("8.8.8.8")
        assert result["name"] == "Clearbit"
        await client.aclose()

    async def test_basic_auth_uses_empty_password(self) -> None:
        """BasicAuth must be constructed with (api_key, '') — empty password."""
        import aiohttp
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key="my_key")
        assert client._auth.login == "my_key"
        assert client._auth.password == ""
        await client.aclose()

    async def test_raise_for_status_401(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        with pytest.raises(ClearbitAuthError):
            await client._raise_for_status(401, "/test")
        await client.aclose()

    async def test_raise_for_status_403(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        with pytest.raises(ClearbitAuthError):
            await client._raise_for_status(403, "/test")
        await client.aclose()

    async def test_raise_for_status_404(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        with pytest.raises(ClearbitNotFoundError):
            await client._raise_for_status(404, "/test")
        await client.aclose()

    async def test_raise_for_status_422(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        with pytest.raises(ClearbitError):
            await client._raise_for_status(422, "/test")
        await client.aclose()

    async def test_raise_for_status_429(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        with pytest.raises(ClearbitRateLimitError):
            await client._raise_for_status(429, "/test")
        await client.aclose()

    async def test_raise_for_status_500(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        with pytest.raises(ClearbitError):
            await client._raise_for_status(500, "/test")
        await client.aclose()

    async def test_202_raises_not_found(self) -> None:
        """202 Accepted (enrichment pending) must raise ClearbitNotFoundError."""
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        mock_resp = self._make_mock_response(202)
        with patch.object(client, "_get_session") as mock_sess:
            mock_sess.return_value.request.return_value.__aenter__ = AsyncMock(
                return_value=mock_resp
            )
            mock_sess.return_value.request.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ClearbitNotFoundError):
                await client.enrich_company("pending.com")
        await client.aclose()

    async def test_aclose_cleans_session(self) -> None:
        from client.http_client import ClearbitHTTPClient
        client = ClearbitHTTPClient(api_key=VALID_API_KEY)
        # Force session creation
        sess = client._get_session()
        assert client._session is not None
        await client.aclose()
        assert client._session is None

    async def test_context_manager(self) -> None:
        from client.http_client import ClearbitHTTPClient
        async with ClearbitHTTPClient(api_key=VALID_API_KEY) as client:
            assert client._api_key == VALID_API_KEY


# ═══════════════════════════════════════════════════════════════════════════════
# 6. CONNECTOR — install()
# ═══════════════════════════════════════════════════════════════════════════════


class TestInstall:
    def _make_connector(self, api_key: str = VALID_API_KEY) -> ClearbitConnector:
        return ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": api_key},
        )

    async def test_install_success(self) -> None:
        connector = self._make_connector()
        connector._make_client = MagicMock(
            return_value=MagicMock(
                get_account_status=AsyncMock(return_value=SAMPLE_COMPANY),
                aclose=AsyncMock(),
            )
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    async def test_install_missing_api_key(self) -> None:
        connector = ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={},
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS
        assert "api_key" in result.message.lower()

    async def test_install_empty_api_key(self) -> None:
        connector = self._make_connector(api_key="")
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_install_auth_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_account_status=AsyncMock(
                side_effect=ClearbitAuthError("Unauthorized", 401)
            ),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_install_not_found_still_healthy(self) -> None:
        """A 404 on the health ping means auth passed — still HEALTHY."""
        connector = self._make_connector()
        mock_client = MagicMock(
            get_account_status=AsyncMock(
                side_effect=ClearbitNotFoundError("company", "clearbit.com")
            ),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_install_network_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_account_status=AsyncMock(
                side_effect=ClearbitNetworkError("Connection refused")
            ),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 7. CONNECTOR — health_check()
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealthCheck:
    def _make_connector(self, api_key: str = VALID_API_KEY) -> ClearbitConnector:
        return ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": api_key},
        )

    async def test_health_check_healthy(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_account_status=AsyncMock(return_value=SAMPLE_COMPANY),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_missing_key(self) -> None:
        connector = self._make_connector(api_key="")
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    async def test_health_check_auth_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_account_status=AsyncMock(
                side_effect=ClearbitAuthError("Forbidden", 403)
            ),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    async def test_health_check_404_still_healthy(self) -> None:
        """A 404 on clearbit.com health ping — auth still valid."""
        connector = self._make_connector()
        mock_client = MagicMock(
            get_account_status=AsyncMock(
                side_effect=ClearbitNotFoundError("company", "clearbit.com")
            ),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    async def test_health_check_network_error(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock(
            get_account_status=AsyncMock(
                side_effect=ClearbitNetworkError("timeout")
            ),
            aclose=AsyncMock(),
        )
        connector._make_client = MagicMock(return_value=mock_client)
        result = await connector.health_check()
        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 8. CONNECTOR — sync()
# ═══════════════════════════════════════════════════════════════════════════════


class TestSync:
    async def test_sync_returns_completed(self) -> None:
        connector = ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )
        result = await connector.sync()
        assert result.status == SyncStatus.COMPLETED

    async def test_sync_returns_zero_documents(self) -> None:
        connector = ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )
        result = await connector.sync()
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.documents_failed == 0

    async def test_sync_message_explains_lookup_only(self) -> None:
        connector = ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )
        result = await connector.sync()
        assert result.message  # non-empty explanatory message
        assert "lookup" in result.message.lower() or "enrich" in result.message.lower()

    async def test_sync_accepts_kwargs(self) -> None:
        """sync() should accept arbitrary kwargs without raising."""
        connector = ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )
        result = await connector.sync(full=True, since="2024-01-01", kb_id="kb-123")
        assert result.status == SyncStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# 9. CONNECTOR — enrich_company()
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnrichCompany:
    def _make_connector(self) -> ClearbitConnector:
        return ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_enrich_company_returns_document(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.enrich_company = AsyncMock(return_value=SAMPLE_COMPANY)
        connector.http_client = mock_client
        doc = await connector.enrich_company("clearbit.com")
        assert isinstance(doc, ConnectorDocument)
        assert doc.metadata["domain"] == "clearbit.com"

    async def test_enrich_company_404_raises(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.enrich_company = AsyncMock(
            side_effect=ClearbitNotFoundError("company", "unknown.xyz")
        )
        connector.http_client = mock_client
        with pytest.raises(ClearbitNotFoundError):
            await connector.enrich_company("unknown.xyz")

    async def test_enrich_company_auth_error_propagates(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.enrich_company = AsyncMock(
            side_effect=ClearbitAuthError("Unauthorized", 401)
        )
        connector.http_client = mock_client
        with pytest.raises(ClearbitAuthError):
            await connector.enrich_company("clearbit.com")

    async def test_enrich_company_stable_id(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.enrich_company = AsyncMock(return_value=SAMPLE_COMPANY)
        connector.http_client = mock_client
        doc = await connector.enrich_company("clearbit.com")
        expected = _stable_id("company", "clearbit.com")
        assert doc.source_id == expected


# ═══════════════════════════════════════════════════════════════════════════════
# 10. CONNECTOR — enrich_person()
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnrichPerson:
    def _make_connector(self) -> ClearbitConnector:
        return ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_enrich_person_returns_document(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.enrich_person = AsyncMock(return_value=SAMPLE_PERSON)
        connector.http_client = mock_client
        doc = await connector.enrich_person("alex@stripe.com")
        assert isinstance(doc, ConnectorDocument)
        assert doc.metadata["email"] == "alex@stripe.com"

    async def test_enrich_person_404_raises(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.enrich_person = AsyncMock(
            side_effect=ClearbitNotFoundError("person", "nobody@nowhere.com")
        )
        connector.http_client = mock_client
        with pytest.raises(ClearbitNotFoundError):
            await connector.enrich_person("nobody@nowhere.com")

    async def test_enrich_person_auth_error_propagates(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.enrich_person = AsyncMock(
            side_effect=ClearbitAuthError("Unauthorized", 401)
        )
        connector.http_client = mock_client
        with pytest.raises(ClearbitAuthError):
            await connector.enrich_person("alex@stripe.com")

    async def test_enrich_person_stable_id(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.enrich_person = AsyncMock(return_value=SAMPLE_PERSON)
        connector.http_client = mock_client
        doc = await connector.enrich_person("alex@stripe.com")
        expected = _stable_id("person", "alex@stripe.com")
        assert doc.source_id == expected


# ═══════════════════════════════════════════════════════════════════════════════
# 11. CONNECTOR — combined_lookup()
# ═══════════════════════════════════════════════════════════════════════════════


class TestCombinedLookup:
    def _make_connector(self) -> ClearbitConnector:
        return ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )

    async def test_combined_lookup_returns_document(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.combined_lookup = AsyncMock(return_value=SAMPLE_COMBINED)
        connector.http_client = mock_client
        doc = await connector.combined_lookup("alex@stripe.com")
        assert isinstance(doc, ConnectorDocument)
        assert doc.metadata["type"] == "combined"

    async def test_combined_lookup_404_raises(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.combined_lookup = AsyncMock(
            side_effect=ClearbitNotFoundError("combined", "nobody@nowhere.com")
        )
        connector.http_client = mock_client
        with pytest.raises(ClearbitNotFoundError):
            await connector.combined_lookup("nobody@nowhere.com")

    async def test_combined_lookup_handles_partial_data(self) -> None:
        """A response with person but no company must not crash."""
        connector = self._make_connector()
        mock_client = MagicMock()
        partial_data = {"person": SAMPLE_PERSON, "company": None}
        mock_client.combined_lookup = AsyncMock(return_value=partial_data)
        connector.http_client = mock_client
        doc = await connector.combined_lookup("alex@stripe.com")
        assert isinstance(doc, ConnectorDocument)
        assert doc.metadata["person_name"] == "Alex Johnson"

    async def test_combined_lookup_stable_id(self) -> None:
        connector = self._make_connector()
        mock_client = MagicMock()
        mock_client.combined_lookup = AsyncMock(return_value=SAMPLE_COMBINED)
        connector.http_client = mock_client
        doc = await connector.combined_lookup("alex@stripe.com")
        expected = _stable_id("combined", "alex@stripe.com")
        assert doc.source_id == expected


# ═══════════════════════════════════════════════════════════════════════════════
# 12. STABLE ID HELPER
# ═══════════════════════════════════════════════════════════════════════════════


class TestStableId:
    def test_stable_id_length_is_16(self) -> None:
        result = _stable_id("company", "example.com")
        assert len(result) == 16

    def test_stable_id_is_hex(self) -> None:
        result = _stable_id("person", "test@test.com")
        assert all(c in "0123456789abcdef" for c in result)

    def test_stable_id_is_deterministic(self) -> None:
        r1 = _stable_id("company", "stripe.com")
        r2 = _stable_id("company", "stripe.com")
        assert r1 == r2

    def test_stable_id_different_for_different_keys(self) -> None:
        r1 = _stable_id("company", "stripe.com")
        r2 = _stable_id("company", "clearbit.com")
        assert r1 != r2

    def test_stable_id_prefix_scopes(self) -> None:
        r1 = _stable_id("company", "example.com")
        r2 = _stable_id("person", "example.com")
        assert r1 != r2


# ═══════════════════════════════════════════════════════════════════════════════
# 13. LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════


class TestLifecycle:
    async def test_aclose_clears_http_client(self) -> None:
        connector = ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )
        connector.http_client = MagicMock(aclose=AsyncMock())
        await connector.aclose()
        assert connector.http_client is None

    async def test_context_manager_closes_on_exit(self) -> None:
        connector = ClearbitConnector(
            tenant_id=TENANT_ID,
            connector_id=CONNECTOR_ID,
            config={"api_key": VALID_API_KEY},
        )
        async with connector:
            connector.http_client = MagicMock(aclose=AsyncMock())
        assert connector.http_client is None

    def test_connector_type(self) -> None:
        connector = ClearbitConnector(config={"api_key": VALID_API_KEY})
        assert connector.CONNECTOR_TYPE == "clearbit"

    def test_auth_type(self) -> None:
        connector = ClearbitConnector(config={"api_key": VALID_API_KEY})
        assert connector.AUTH_TYPE == "api_key"

    def test_constructor_stores_api_key(self) -> None:
        connector = ClearbitConnector(config={"api_key": "mykey123"})
        assert connector._api_key == "mykey123"
