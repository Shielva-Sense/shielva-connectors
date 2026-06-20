"""Unit tests for LinkedInConnector — all LinkedIn HTTP calls are mocked.

Covers:
- Class attributes (CONNECTOR_TYPE, AUTH_TYPE)
- All exception types and their attributes
- All model enum values and dataclass fields (including HealthCheckResult.name)
- Normalizer functions for profiles and posts (full and minimal records)
- Stable source_id generation (SHA-256[:16])
- _localized_string helper
- Retry logic (success, retry-on-error, auth-error short-circuits, rate-limit)
- install() — missing creds, with access_token success, auth error, generic exception, no token success
- authorize() — URL construction, with and without redirect_uri, scope presence
- health_check() — success (with name), auth error, network error, missing creds, no access token, generic exception
- sync() — no token, profile fetch failure, profile + posts, post normalize failure, COMPLETED vs PARTIAL
- get_profile, get_email, list_posts, get_organization, list_organization_posts
- aclose / context manager
- CircuitBreaker — threshold, reset, half-open, is_open
- _ensure_client, _has_credentials
- _fetch_posts handles missing elements key
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from connector import LinkedInConnector
from exceptions import (
    LinkedInAuthError,
    LinkedInError,
    LinkedInNetworkError,
    LinkedInNotFoundError,
    LinkedInRateLimitError,
    LinkedInServerError,
)
from helpers.utils import (
    CircuitBreaker,
    _localized_string,
    _stable_id,
    normalize_post,
    normalize_profile,
    with_retry,
)
from models import (
    AuthStatus,
    ConnectorDocument,
    ConnectorHealth,
    HealthCheckResult,
    InstallResult,
    SyncResult,
    SyncStatus,
)

TENANT_ID = "tenant_test_linkedin_001"
CONNECTOR_ID = "conn_linkedin_test_001"
VALID_CLIENT_ID = "86abc123clientid"
VALID_CLIENT_SECRET = "supersecret_linkedin_value"
VALID_ACCESS_TOKEN = "AQXNnd2kXP8P4n0BI_linkedin_test_token"

# ── Sample fixtures ──────────────────────────────────────────────────────────

LOCALIZED_EN_US_FIRST: dict[str, Any] = {
    "localized": {"en_US": "Jane"},
    "preferredLocale": {"country": "US", "language": "en"},
}

LOCALIZED_EN_US_LAST: dict[str, Any] = {
    "localized": {"en_US": "Doe"},
    "preferredLocale": {"country": "US", "language": "en"},
}

LOCALIZED_HEADLINE: dict[str, Any] = {
    "localized": {"en_US": "Software Engineer at Shielva"},
    "preferredLocale": {"country": "US", "language": "en"},
}

SAMPLE_PROFILE: dict[str, Any] = {
    "id": "person_abc123",
    "firstName": LOCALIZED_EN_US_FIRST,
    "lastName": LOCALIZED_EN_US_LAST,
    "headline": LOCALIZED_HEADLINE,
    "profilePicture": {"displayImage": "urn:li:digitalmediaAsset:pic123"},
}

SAMPLE_EMAIL_DATA: dict[str, Any] = {
    "elements": [
        {
            "handle": "urn:li:emailAddress:12345",
            "handle~": {"emailAddress": "jane.doe@example.com"},
        }
    ]
}

SAMPLE_POST: dict[str, Any] = {
    "id": "share_789xyz",
    "author": "urn:li:person:person_abc123",
    "text": {"text": "Excited to share our latest product update!"},
    "created": {"time": 1717200000000},
    "lastModified": {"time": 1717200300000},
    "visibility": {
        "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
    },
    "activity": "urn:li:activity:987654",
}

SAMPLE_POST_MINIMAL: dict[str, Any] = {
    "id": "share_minimal",
    "author": "urn:li:person:person_abc123",
}

SAMPLE_ORGANIZATION: dict[str, Any] = {
    "id": 123456,
    "localizedName": "Shielva AI",
    "vanityName": "shielva-ai",
    "websiteUrl": "https://shielva.ai",
}

POSTS_RESPONSE: dict[str, Any] = {
    "elements": [SAMPLE_POST],
    "paging": {"count": 50, "start": 0, "total": 1},
}

EMPTY_POSTS_RESPONSE: dict[str, Any] = {
    "elements": [],
    "paging": {"count": 50, "start": 0, "total": 0},
}

ORG_POSTS_RESPONSE: dict[str, Any] = {
    "elements": [SAMPLE_POST],
    "paging": {"count": 50, "start": 0, "total": 1},
}

ME_PROFILE_RESPONSE = SAMPLE_PROFILE


# ── Helper: build a connector ─────────────────────────────────────────────────

def _make_connector(
    client_id: str = VALID_CLIENT_ID,
    client_secret: str = VALID_CLIENT_SECRET,
    access_token: str = VALID_ACCESS_TOKEN,
    redirect_uri: str = "",
) -> LinkedInConnector:
    return LinkedInConnector(
        tenant_id=TENANT_ID,
        connector_id=CONNECTOR_ID,
        config={
            "client_id": client_id,
            "client_secret": client_secret,
            "access_token": access_token,
            "redirect_uri": redirect_uri,
        },
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Class attributes
# ═══════════════════════════════════════════════════════════════════════════════

class TestClassAttributes:
    def test_connector_type(self) -> None:
        c = _make_connector()
        assert c.CONNECTOR_TYPE == "linkedin"

    def test_auth_type(self) -> None:
        c = _make_connector()
        assert c.AUTH_TYPE == "oauth2"

    def test_tenant_id_stored(self) -> None:
        c = _make_connector()
        assert c.tenant_id == TENANT_ID

    def test_connector_id_stored(self) -> None:
        c = _make_connector()
        assert c.connector_id == CONNECTOR_ID


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Exceptions
# ═══════════════════════════════════════════════════════════════════════════════

class TestExceptions:
    def test_base_error(self) -> None:
        e = LinkedInError("msg", 400, "bad")
        assert str(e) == "msg"
        assert e.status_code == 400
        assert e.code == "bad"

    def test_auth_error(self) -> None:
        e = LinkedInAuthError("unauthorized", 401, "unauthorized")
        assert isinstance(e, LinkedInError)
        assert e.status_code == 401

    def test_rate_limit_error(self) -> None:
        e = LinkedInRateLimitError("too many", retry_after=60.0)
        assert isinstance(e, LinkedInError)
        assert e.status_code == 429
        assert e.code == "rate_limit"
        assert e.retry_after == 60.0

    def test_rate_limit_default_retry_after(self) -> None:
        e = LinkedInRateLimitError("rate")
        assert e.retry_after == 0.0

    def test_not_found_error(self) -> None:
        e = LinkedInNotFoundError("profile", "/me")
        assert isinstance(e, LinkedInError)
        assert e.status_code == 404
        assert e.code == "resource_missing"
        assert "profile" in str(e)

    def test_network_error(self) -> None:
        e = LinkedInNetworkError("timeout")
        assert isinstance(e, LinkedInError)

    def test_server_error(self) -> None:
        e = LinkedInServerError("internal error", 503)
        assert isinstance(e, LinkedInError)
        assert e.status_code == 503


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Models
# ═══════════════════════════════════════════════════════════════════════════════

class TestModels:
    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_auth_status_values(self) -> None:
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
        r = InstallResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED)
        assert r.connector_id == ""
        assert r.message == ""

    def test_health_check_result_has_name(self) -> None:
        r = HealthCheckResult(health=ConnectorHealth.HEALTHY, auth_status=AuthStatus.CONNECTED, name="Jane Doe")
        assert r.name == "Jane Doe"

    def test_sync_result_defaults(self) -> None:
        r = SyncResult(status=SyncStatus.COMPLETED)
        assert r.documents_found == 0
        assert r.documents_synced == 0
        assert r.documents_failed == 0

    def test_connector_document_fields(self) -> None:
        doc = ConnectorDocument(
            source_id="abc123",
            title="Test",
            content="Content",
            connector_id=CONNECTOR_ID,
            tenant_id=TENANT_ID,
        )
        assert doc.source_url == ""
        assert doc.metadata == {}


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _localized_string helper
# ═══════════════════════════════════════════════════════════════════════════════

class TestLocalizedString:
    def test_en_us_locale(self) -> None:
        obj = {"localized": {"en_US": "Hello"}}
        assert _localized_string(obj) == "Hello"

    def test_preferred_locale_first(self) -> None:
        obj = {"localized": {"en_US": "Hello", "fr_FR": "Bonjour"}}
        assert _localized_string(obj, "en_US") == "Hello"

    def test_fallback_to_any_locale(self) -> None:
        obj = {"localized": {"de_DE": "Hallo"}}
        result = _localized_string(obj, "en_US")
        assert result == "Hallo"

    def test_non_dict_returns_str(self) -> None:
        assert _localized_string("plain") == "plain"

    def test_empty_dict_returns_empty(self) -> None:
        assert _localized_string({}) == ""

    def test_none_returns_empty(self) -> None:
        assert _localized_string(None) == ""  # type: ignore[arg-type]

    def test_localized_missing_returns_empty(self) -> None:
        obj = {"other": "value"}
        assert _localized_string(obj) == ""


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Normalizers
# ═══════════════════════════════════════════════════════════════════════════════

class TestNormalizeProfile:
    def test_stable_id(self) -> None:
        doc = normalize_profile(SAMPLE_PROFILE, SAMPLE_EMAIL_DATA, CONNECTOR_ID, TENANT_ID)
        expected = hashlib.sha256(b"profile:person_abc123").hexdigest()[:16]
        assert doc.source_id == expected

    def test_title_contains_name(self) -> None:
        doc = normalize_profile(SAMPLE_PROFILE, SAMPLE_EMAIL_DATA, CONNECTOR_ID, TENANT_ID)
        assert "Jane" in doc.title
        assert "Doe" in doc.title

    def test_content_contains_headline(self) -> None:
        doc = normalize_profile(SAMPLE_PROFILE, SAMPLE_EMAIL_DATA, CONNECTOR_ID, TENANT_ID)
        assert "Software Engineer" in doc.content

    def test_content_contains_email(self) -> None:
        doc = normalize_profile(SAMPLE_PROFILE, SAMPLE_EMAIL_DATA, CONNECTOR_ID, TENANT_ID)
        assert "jane.doe@example.com" in doc.content

    def test_metadata_entity_type(self) -> None:
        doc = normalize_profile(SAMPLE_PROFILE, SAMPLE_EMAIL_DATA, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["entity_type"] == "profile"

    def test_metadata_author_urn(self) -> None:
        doc = normalize_profile(SAMPLE_PROFILE, SAMPLE_EMAIL_DATA, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["author_urn"] == "urn:li:person:person_abc123"

    def test_source_url_contains_person_id(self) -> None:
        doc = normalize_profile(SAMPLE_PROFILE, SAMPLE_EMAIL_DATA, CONNECTOR_ID, TENANT_ID)
        assert "person_abc123" in doc.source_url

    def test_connector_and_tenant_ids(self) -> None:
        doc = normalize_profile(SAMPLE_PROFILE, SAMPLE_EMAIL_DATA, CONNECTOR_ID, TENANT_ID)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_minimal_profile_no_email(self) -> None:
        minimal = {"id": "minperson"}
        doc = normalize_profile(minimal, {}, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == hashlib.sha256(b"profile:minperson").hexdigest()[:16]

    def test_empty_email_data_ok(self) -> None:
        doc = normalize_profile(SAMPLE_PROFILE, {}, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["email"] == ""


class TestNormalizePost:
    def test_stable_id(self) -> None:
        doc = normalize_post(SAMPLE_POST, CONNECTOR_ID, TENANT_ID)
        expected = hashlib.sha256(b"post:share_789xyz").hexdigest()[:16]
        assert doc.source_id == expected

    def test_title_truncates_long_text(self) -> None:
        long_post = dict(SAMPLE_POST)
        long_post["text"] = {"text": "A" * 100}
        doc = normalize_post(long_post, CONNECTOR_ID, TENANT_ID)
        assert "..." in doc.title

    def test_title_short_text_no_truncate(self) -> None:
        doc = normalize_post(SAMPLE_POST, CONNECTOR_ID, TENANT_ID)
        assert "..." not in doc.title

    def test_content_contains_author(self) -> None:
        doc = normalize_post(SAMPLE_POST, CONNECTOR_ID, TENANT_ID)
        assert "urn:li:person:person_abc123" in doc.content

    def test_content_contains_text(self) -> None:
        doc = normalize_post(SAMPLE_POST, CONNECTOR_ID, TENANT_ID)
        assert "Excited to share" in doc.content

    def test_metadata_entity_type(self) -> None:
        doc = normalize_post(SAMPLE_POST, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["entity_type"] == "post"

    def test_metadata_share_id(self) -> None:
        doc = normalize_post(SAMPLE_POST, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["share_id"] == "share_789xyz"

    def test_source_url_contains_share_id(self) -> None:
        doc = normalize_post(SAMPLE_POST, CONNECTOR_ID, TENANT_ID)
        assert "share_789xyz" in doc.source_url

    def test_connector_and_tenant_ids(self) -> None:
        doc = normalize_post(SAMPLE_POST, CONNECTOR_ID, TENANT_ID)
        assert doc.connector_id == CONNECTOR_ID
        assert doc.tenant_id == TENANT_ID

    def test_minimal_post_no_crash(self) -> None:
        doc = normalize_post(SAMPLE_POST_MINIMAL, CONNECTOR_ID, TENANT_ID)
        assert doc.source_id == hashlib.sha256(b"post:share_minimal").hexdigest()[:16]

    def test_metadata_visibility(self) -> None:
        doc = normalize_post(SAMPLE_POST, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["visibility"] == "PUBLIC"

    def test_metadata_created_at(self) -> None:
        doc = normalize_post(SAMPLE_POST, CONNECTOR_ID, TENANT_ID)
        assert doc.metadata["created_at"] == "1717200000000"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. _stable_id
# ═══════════════════════════════════════════════════════════════════════════════

class TestStableId:
    def test_length_is_16(self) -> None:
        assert len(_stable_id("profile", "abc")) == 16

    def test_deterministic(self) -> None:
        assert _stable_id("post", "x") == _stable_id("post", "x")

    def test_different_types_differ(self) -> None:
        assert _stable_id("profile", "abc") != _stable_id("post", "abc")

    def test_sha256_prefix(self) -> None:
        expected = hashlib.sha256(b"post:share123").hexdigest()[:16]
        assert _stable_id("post", "share123") == expected


# ═══════════════════════════════════════════════════════════════════════════════
# 7. CircuitBreaker
# ═══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    def test_initial_state_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.state == "closed"
        assert not cb.is_open

    def test_opens_after_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.on_failure()
        assert cb.is_open

    def test_not_open_before_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.on_failure()
        cb.on_failure()
        assert not cb.is_open

    def test_reset_on_success(self) -> None:
        cb = CircuitBreaker(failure_threshold=2)
        cb.on_failure()
        cb.on_failure()
        assert cb.is_open
        cb.on_success()
        assert not cb.is_open
        assert cb.state == "closed"

    def test_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout_s=0.0)
        cb.on_failure()
        assert cb.state == "half-open"

    def test_counts_failures(self) -> None:
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(4):
            cb.on_failure()
        assert not cb.is_open
        cb.on_failure()
        assert cb.is_open


# ═══════════════════════════════════════════════════════════════════════════════
# 8. with_retry
# ═══════════════════════════════════════════════════════════════════════════════

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_first_attempt(self) -> None:
        fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(fn)
        assert result == {"ok": True}
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_linkedin_error(self) -> None:
        fn = AsyncMock(side_effect=[LinkedInError("transient"), {"ok": True}])
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(fn, max_retries=3)
        assert result == {"ok": True}
        assert fn.call_count == 2

    @pytest.mark.asyncio
    async def test_auth_error_not_retried(self) -> None:
        fn = AsyncMock(side_effect=LinkedInAuthError("bad token"))
        with pytest.raises(LinkedInAuthError):
            await with_retry(fn, max_retries=3)
        assert fn.call_count == 1

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self) -> None:
        err = LinkedInError("persistent failure")
        fn = AsyncMock(side_effect=err)
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(LinkedInError):
                await with_retry(fn, max_retries=3)
        assert fn.call_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_honours_retry_after(self) -> None:
        fn = AsyncMock(side_effect=[LinkedInRateLimitError("rate", retry_after=1.0), {"ok": True}])
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            result = await with_retry(fn, max_retries=3)
        assert result == {"ok": True}
        sleep_mock.assert_called_once_with(1.0)

    @pytest.mark.asyncio
    async def test_rate_limit_uses_backoff_when_no_retry_after(self) -> None:
        fn = AsyncMock(side_effect=[LinkedInRateLimitError("rate", retry_after=0.0), {"ok": True}])
        sleep_mock = AsyncMock()
        with patch("helpers.utils.asyncio.sleep", sleep_mock):
            await with_retry(fn, max_retries=3)
        assert sleep_mock.call_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 9. install()
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstall:
    @pytest.mark.asyncio
    async def test_missing_client_id(self) -> None:
        c = LinkedInConnector(config={"client_secret": VALID_CLIENT_SECRET})
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_missing_client_secret(self) -> None:
        c = LinkedInConnector(config={"client_id": VALID_CLIENT_ID})
        result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_no_access_token_returns_healthy(self) -> None:
        c = LinkedInConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
        result = await c.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "OAuth" in result.message

    @pytest.mark.asyncio
    async def test_with_access_token_success(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(return_value=SAMPLE_PROFILE)
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_with_access_token_auth_error(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(side_effect=LinkedInAuthError("bad token"))
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_with_access_token_generic_error(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(side_effect=Exception("network down"))
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════════════
# 10. authorize()
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuthorize:
    def test_returns_string(self) -> None:
        c = _make_connector()
        url = c.authorize()
        assert isinstance(url, str)

    def test_contains_auth_base_url(self) -> None:
        c = _make_connector()
        url = c.authorize()
        assert "linkedin.com/oauth/v2/authorization" in url

    def test_contains_client_id(self) -> None:
        c = _make_connector()
        url = c.authorize()
        assert VALID_CLIENT_ID in url

    def test_contains_response_type(self) -> None:
        c = _make_connector()
        url = c.authorize()
        assert "response_type=code" in url

    def test_contains_scope(self) -> None:
        c = _make_connector()
        url = c.authorize()
        assert "r_liteprofile" in url

    def test_contains_redirect_uri_when_set(self) -> None:
        c = _make_connector(redirect_uri="https://app.shielva.ai/callback")
        url = c.authorize()
        assert "redirect_uri" in url
        assert "shielva" in url

    def test_no_redirect_uri_when_not_set(self) -> None:
        c = _make_connector(redirect_uri="")
        url = c.authorize()
        assert "redirect_uri" not in url


# ═══════════════════════════════════════════════════════════════════════════════
# 11. health_check()
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_missing_creds(self) -> None:
        c = LinkedInConnector()
        result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_no_access_token(self) -> None:
        c = LinkedInConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
        result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_success_with_name(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(return_value=SAMPLE_PROFILE)
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.health_check()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.name == "Jane Doe"
        assert "Jane Doe" in result.message

    @pytest.mark.asyncio
    async def test_auth_error(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(side_effect=LinkedInAuthError("expired"))
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.health_check()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_network_error_degraded(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(side_effect=LinkedInNetworkError("timeout"))
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.health_check()
        assert result.health in (ConnectorHealth.DEGRADED, ConnectorHealth.OFFLINE)
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_generic_error_degraded(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(side_effect=Exception("unexpected"))
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            result = await c.health_check()
        assert result.health == ConnectorHealth.DEGRADED

    @pytest.mark.asyncio
    async def test_circuit_breaker_updated_on_success(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(return_value=SAMPLE_PROFILE)
        mock_client.aclose = AsyncMock()
        with patch.object(c, "_make_client", return_value=mock_client):
            await c.health_check()
        assert not c._circuit_breaker.is_open


# ═══════════════════════════════════════════════════════════════════════════════
# 12. sync()
# ═══════════════════════════════════════════════════════════════════════════════

class TestSync:
    @pytest.mark.asyncio
    async def test_no_access_token_returns_failed(self) -> None:
        c = LinkedInConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
        result = await c.sync()
        assert result.status == SyncStatus.FAILED

    @pytest.mark.asyncio
    async def test_profile_and_posts_success(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(return_value=SAMPLE_PROFILE)
        mock_client.get_email = AsyncMock(return_value=SAMPLE_EMAIL_DATA)
        mock_client.list_posts = AsyncMock(return_value=POSTS_RESPONSE)
        c.http_client = mock_client
        result = await c.sync()
        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found >= 2  # profile + 1 post
        assert result.documents_synced >= 2

    @pytest.mark.asyncio
    async def test_profile_fetch_failure(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(side_effect=LinkedInError("profile unavailable"))
        mock_client.get_email = AsyncMock(return_value={})
        c.http_client = mock_client
        result = await c.sync()
        assert result.documents_failed >= 1

    @pytest.mark.asyncio
    async def test_posts_unavailable_non_fatal(self) -> None:
        """Posts fetch failure should not fail the whole sync if profile succeeded."""
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(return_value=SAMPLE_PROFILE)
        mock_client.get_email = AsyncMock(return_value=SAMPLE_EMAIL_DATA)
        mock_client.list_posts = AsyncMock(side_effect=LinkedInError("scope missing"))
        c.http_client = mock_client
        result = await c.sync()
        # Profile was synced, posts were skipped gracefully
        assert result.documents_synced >= 1

    @pytest.mark.asyncio
    async def test_post_normalize_failure_increments_failed(self) -> None:
        """Patch normalize_post to raise so the except clause increments failed."""
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(return_value=SAMPLE_PROFILE)
        mock_client.get_email = AsyncMock(return_value=SAMPLE_EMAIL_DATA)
        bad_posts = {"elements": [{"id": "bad_post"}]}
        mock_client.list_posts = AsyncMock(return_value=bad_posts)
        c.http_client = mock_client
        with patch("connector.normalize_post", side_effect=ValueError("bad data")):
            result = await c.sync()
        # Profile synced, bad post increments failed
        assert result.documents_failed >= 1
        assert result.status == SyncStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_completed_when_no_failures(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(return_value=SAMPLE_PROFILE)
        mock_client.get_email = AsyncMock(return_value=SAMPLE_EMAIL_DATA)
        mock_client.list_posts = AsyncMock(return_value=EMPTY_POSTS_RESPONSE)
        c.http_client = mock_client
        result = await c.sync()
        assert result.status == SyncStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_creates_http_client_if_none(self) -> None:
        c = _make_connector()
        assert c.http_client is None
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(return_value=SAMPLE_PROFILE)
        mock_client.get_email = AsyncMock(return_value=SAMPLE_EMAIL_DATA)
        mock_client.list_posts = AsyncMock(return_value=EMPTY_POSTS_RESPONSE)
        with patch.object(c, "_make_client", return_value=mock_client):
            await c.sync()
        assert c.http_client is mock_client


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Direct API methods
# ═══════════════════════════════════════════════════════════════════════════════

class TestDirectApiMethods:
    @pytest.mark.asyncio
    async def test_get_profile(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(return_value=SAMPLE_PROFILE)
        c.http_client = mock_client
        result = await c.get_profile()
        assert result["id"] == "person_abc123"

    @pytest.mark.asyncio
    async def test_get_email(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_email = AsyncMock(return_value=SAMPLE_EMAIL_DATA)
        c.http_client = mock_client
        result = await c.get_email()
        assert "elements" in result

    @pytest.mark.asyncio
    async def test_list_posts(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.list_posts = AsyncMock(return_value=POSTS_RESPONSE)
        c.http_client = mock_client
        result = await c.list_posts("urn:li:person:person_abc123", count=50)
        assert result["elements"][0]["id"] == "share_789xyz"

    @pytest.mark.asyncio
    async def test_get_organization(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.get_organization = AsyncMock(return_value=SAMPLE_ORGANIZATION)
        c.http_client = mock_client
        result = await c.get_organization("123456")
        assert result["localizedName"] == "Shielva AI"

    @pytest.mark.asyncio
    async def test_list_organization_posts(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.list_organization_posts = AsyncMock(return_value=ORG_POSTS_RESPONSE)
        c.http_client = mock_client
        result = await c.list_organization_posts("urn:li:organization:123456")
        assert len(result["elements"]) == 1

    @pytest.mark.asyncio
    async def test_get_profile_creates_client(self) -> None:
        c = _make_connector()
        assert c.http_client is None
        mock_client = MagicMock()
        mock_client.get_profile = AsyncMock(return_value=SAMPLE_PROFILE)
        with patch.object(c, "_make_client", return_value=mock_client):
            await c.get_profile()
        assert c.http_client is mock_client


# ═══════════════════════════════════════════════════════════════════════════════
# 14. _has_credentials
# ═══════════════════════════════════════════════════════════════════════════════

class TestHasCredentials:
    def test_true_with_access_token(self) -> None:
        c = LinkedInConnector(config={"access_token": VALID_ACCESS_TOKEN})
        assert c._has_credentials()

    def test_true_with_client_creds(self) -> None:
        c = LinkedInConnector(config={"client_id": VALID_CLIENT_ID, "client_secret": VALID_CLIENT_SECRET})
        assert c._has_credentials()

    def test_false_with_nothing(self) -> None:
        c = LinkedInConnector()
        assert not c._has_credentials()

    def test_false_with_only_client_id(self) -> None:
        c = LinkedInConnector(config={"client_id": VALID_CLIENT_ID})
        assert not c._has_credentials()

    def test_false_with_only_client_secret(self) -> None:
        c = LinkedInConnector(config={"client_secret": VALID_CLIENT_SECRET})
        assert not c._has_credentials()


# ═══════════════════════════════════════════════════════════════════════════════
# 15. _ensure_client
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnsureClient:
    def test_creates_client_when_none(self) -> None:
        c = _make_connector()
        assert c.http_client is None
        client = c._ensure_client()
        assert client is not None
        assert c.http_client is client

    def test_reuses_existing_client(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        c.http_client = mock_client
        result = c._ensure_client()
        assert result is mock_client


# ═══════════════════════════════════════════════════════════════════════════════
# 16. aclose / context manager
# ═══════════════════════════════════════════════════════════════════════════════

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_closes_client(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        c.http_client = mock_client
        await c.aclose()
        mock_client.aclose.assert_called_once()
        assert c.http_client is None

    @pytest.mark.asyncio
    async def test_aclose_no_client_no_error(self) -> None:
        c = _make_connector()
        await c.aclose()  # Should not raise

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        c.http_client = mock_client
        async with c:
            pass
        mock_client.aclose.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 17. _fetch_posts edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestFetchPostsEdgeCases:
    @pytest.mark.asyncio
    async def test_missing_elements_key(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.list_posts = AsyncMock(return_value={})
        c.http_client = mock_client
        posts = await c._fetch_posts("urn:li:person:abc")
        assert posts == []

    @pytest.mark.asyncio
    async def test_filters_non_dict_elements(self) -> None:
        c = _make_connector()
        mock_client = MagicMock()
        mock_client.list_posts = AsyncMock(return_value={"elements": [None, "bad", SAMPLE_POST]})
        c.http_client = mock_client
        posts = await c._fetch_posts("urn:li:person:abc")
        assert len(posts) == 1
        assert posts[0]["id"] == "share_789xyz"
