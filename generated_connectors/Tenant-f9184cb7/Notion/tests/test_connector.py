"""Tests for the Notion connector — no live API calls."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_pkg = Path(__file__).parent.parent
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from exceptions import (
    NotionAuthError,
    NotionError,
    NotionNetworkError,
    NotionNotFoundError,
    NotionRateLimitError,
)
from models import (
    AuthStatus,
    ConnectorHealth,
    ConnectorDocument,
    InstallResult,
    HealthCheckResult,
    SyncResult,
    SyncStatus,
    NotionObjectType,
)
from helpers.utils import (
    normalize_page,
    normalize_database,
    with_retry,
    _extract_title,
    _extract_rich_text,
    _block_to_text,
)
from client.http_client import NotionHTTPClient
from connector import NotionConnector

TENANT = "Tenant-f9184cb7"
CONNECTOR_ID = "notion_test"
TOKEN = "secret_test_integration_token"

PAGE_ID = "page-1234-abcd-5678-efgh"
DATABASE_ID = "db-1234-abcd-5678-efgh"


# ── fixtures ──────────────────────────────────────────────────────────────────

def _make_page(
    page_id: str = PAGE_ID,
    title: str = "My Test Page",
    url: str = "https://www.notion.so/my-test-page",
    archived: bool = False,
    parent_type: str = "workspace",
) -> Dict[str, Any]:
    return {
        "object": "page",
        "id": page_id,
        "url": url,
        "created_time": "2024-01-01T00:00:00.000Z",
        "last_edited_time": "2024-06-01T00:00:00.000Z",
        "archived": archived,
        "parent": {"type": parent_type},
        "properties": {
            "title": {
                "type": "title",
                "title": [{"plain_text": title, "type": "text"}],
            }
        },
    }


def _make_database(
    db_id: str = DATABASE_ID,
    title: str = "My Test Database",
    url: str = "https://www.notion.so/my-test-db",
) -> Dict[str, Any]:
    return {
        "object": "database",
        "id": db_id,
        "url": url,
        "created_time": "2024-01-01T00:00:00.000Z",
        "last_edited_time": "2024-06-01T00:00:00.000Z",
        "archived": False,
        "title": [{"plain_text": title, "type": "text"}],
        "properties": {
            "Name": {"type": "title"},
            "Status": {"type": "select"},
            "Due": {"type": "date"},
        },
    }


def _make_block(
    block_type: str = "paragraph",
    text: str = "Hello world",
    has_children: bool = False,
    block_id: str = "block-abc-123",
) -> Dict[str, Any]:
    return {
        "object": "block",
        "id": block_id,
        "type": block_type,
        "has_children": has_children,
        block_type: {
            "rich_text": [{"plain_text": text, "type": "text"}]
        },
    }


def _make_search_response(
    results: List[Dict[str, Any]],
    has_more: bool = False,
    next_cursor: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "object": "list",
        "results": results,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def _make_block_response(
    blocks: List[Dict[str, Any]],
    has_more: bool = False,
    next_cursor: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "object": "list",
        "results": blocks,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def _make_connector(token: str = TOKEN) -> NotionConnector:
    return NotionConnector(
        tenant_id=TENANT,
        connector_id=CONNECTOR_ID,
        config={"api_key": token},
    )


def _mock_session_get(response_data: Dict[str, Any], status: int = 200) -> MagicMock:
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=response_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _mock_session_post(response_data: Dict[str, Any], status: int = 200) -> MagicMock:
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=response_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


# ── 1. Exception hierarchy ────────────────────────────────────────────────────

class TestExceptions:
    def test_notion_error_is_base_exception(self) -> None:
        exc = NotionError("base error")
        assert isinstance(exc, Exception)
        assert str(exc) == "base error"

    def test_auth_error_is_notion_error(self) -> None:
        exc = NotionAuthError("unauthorized")
        assert isinstance(exc, NotionError)

    def test_network_error_is_notion_error(self) -> None:
        exc = NotionNetworkError("timeout")
        assert isinstance(exc, NotionError)

    def test_rate_limit_error_is_notion_error(self) -> None:
        exc = NotionRateLimitError("429")
        assert isinstance(exc, NotionError)

    def test_not_found_error_is_notion_error(self) -> None:
        exc = NotionNotFoundError("not found")
        assert isinstance(exc, NotionError)

    def test_exception_hierarchy_distinct_types(self) -> None:
        assert NotionAuthError is not NotionNetworkError
        assert NotionRateLimitError is not NotionNotFoundError

    def test_all_subclasses_catchable_as_notion_error(self) -> None:
        for exc_class in [NotionAuthError, NotionNetworkError, NotionRateLimitError, NotionNotFoundError]:
            exc = exc_class("test")
            assert isinstance(exc, NotionError)


# ── 2. Models ─────────────────────────────────────────────────────────────────

class TestModels:
    def test_auth_status_values(self) -> None:
        assert AuthStatus.CONNECTED == "connected"
        assert AuthStatus.FAILED == "failed"
        assert AuthStatus.MISSING_CREDENTIALS == "missing_credentials"
        assert AuthStatus.INVALID_CREDENTIALS == "invalid_credentials"

    def test_connector_health_values(self) -> None:
        assert ConnectorHealth.HEALTHY == "healthy"
        assert ConnectorHealth.DEGRADED == "degraded"
        assert ConnectorHealth.OFFLINE == "offline"

    def test_sync_status_values(self) -> None:
        assert SyncStatus.COMPLETED == "completed"
        assert SyncStatus.PARTIAL == "partial"
        assert SyncStatus.FAILED == "failed"

    def test_notion_object_type_values(self) -> None:
        assert NotionObjectType.PAGE == "page"
        assert NotionObjectType.DATABASE == "database"
        assert NotionObjectType.BLOCK == "block"

    def test_connector_document_defaults(self) -> None:
        doc = ConnectorDocument(id="abc", title="Test", content="body")
        assert doc.type == "notion_page"
        assert doc.metadata == {}

    def test_install_result_fields(self) -> None:
        result = InstallResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            connector_id=CONNECTOR_ID,
            message="ok",
        )
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    def test_health_check_result_fields(self) -> None:
        result = HealthCheckResult(
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.CONNECTED,
            message="Connected — bot: My Bot",
        )
        assert "My Bot" in result.message

    def test_sync_result_defaults(self) -> None:
        result = SyncResult(status=SyncStatus.COMPLETED)
        assert result.documents_found == 0
        assert result.documents_synced == 0
        assert result.documents_failed == 0
        assert result.message == ""


# ── 3. Normalizer utils ───────────────────────────────────────────────────────

class TestExtractTitle:
    def test_extracts_title_from_title_property(self) -> None:
        properties = {
            "title": {
                "type": "title",
                "title": [{"plain_text": "My Page", "type": "text"}],
            }
        }
        assert _extract_title(properties) == "My Page"

    def test_extracts_title_from_name_property(self) -> None:
        properties = {
            "Name": {
                "type": "title",
                "title": [{"plain_text": "Named Page", "type": "text"}],
            }
        }
        assert _extract_title(properties) == "Named Page"

    def test_extracts_multipart_title(self) -> None:
        properties = {
            "Name": {
                "type": "title",
                "title": [
                    {"plain_text": "Hello ", "type": "text"},
                    {"plain_text": "World", "type": "text"},
                ],
            }
        }
        assert _extract_title(properties) == "Hello World"

    def test_returns_untitled_when_no_title_property(self) -> None:
        properties = {"Status": {"type": "select"}}
        assert _extract_title(properties) == "Untitled"

    def test_returns_untitled_on_empty_dict(self) -> None:
        assert _extract_title({}) == "Untitled"

    def test_handles_empty_title_array(self) -> None:
        properties = {"title": {"type": "title", "title": []}}
        assert _extract_title(properties) == ""


class TestExtractRichText:
    def test_concatenates_plain_text(self) -> None:
        rich_text = [
            {"plain_text": "Hello ", "type": "text"},
            {"plain_text": "world", "type": "text"},
        ]
        assert _extract_rich_text(rich_text) == "Hello world"

    def test_handles_empty_array(self) -> None:
        assert _extract_rich_text([]) == ""

    def test_skips_non_dict_items(self) -> None:
        rich_text: List[Any] = [{"plain_text": "ok"}, "not_a_dict"]
        assert _extract_rich_text(rich_text) == "ok"


class TestBlockToText:
    def test_paragraph_block(self) -> None:
        block = _make_block("paragraph", "Some text")
        assert _block_to_text(block) == "Some text"

    def test_heading_1_block(self) -> None:
        block = _make_block("heading_1", "Big Heading")
        assert _block_to_text(block) == "# Big Heading"

    def test_heading_2_block(self) -> None:
        block = _make_block("heading_2", "Medium Heading")
        assert _block_to_text(block) == "## Medium Heading"

    def test_heading_3_block(self) -> None:
        block = _make_block("heading_3", "Small Heading")
        assert _block_to_text(block) == "### Small Heading"

    def test_bulleted_list_block(self) -> None:
        block = _make_block("bulleted_list_item", "Bullet point")
        assert _block_to_text(block) == "- Bullet point"

    def test_numbered_list_block(self) -> None:
        block = _make_block("numbered_list_item", "Step one")
        assert _block_to_text(block) == "1. Step one"

    def test_todo_unchecked(self) -> None:
        block = {
            "object": "block", "id": "b1", "type": "to_do", "has_children": False,
            "to_do": {"rich_text": [{"plain_text": "Task", "type": "text"}], "checked": False},
        }
        assert _block_to_text(block) == "[ ] Task"

    def test_todo_checked(self) -> None:
        block = {
            "object": "block", "id": "b1", "type": "to_do", "has_children": False,
            "to_do": {"rich_text": [{"plain_text": "Done task", "type": "text"}], "checked": True},
        }
        assert _block_to_text(block) == "[x] Done task"

    def test_quote_block(self) -> None:
        block = _make_block("quote", "Quoted text")
        assert _block_to_text(block) == "> Quoted text"

    def test_code_block(self) -> None:
        block = {
            "object": "block", "id": "b1", "type": "code", "has_children": False,
            "code": {
                "rich_text": [{"plain_text": "print('hi')", "type": "text"}],
                "language": "python",
            },
        }
        result = _block_to_text(block)
        assert "```python" in result
        assert "print('hi')" in result

    def test_divider_block(self) -> None:
        block = {"object": "block", "id": "b1", "type": "divider", "has_children": False, "divider": {}}
        assert _block_to_text(block) == "---"

    def test_unknown_block_type_returns_empty(self) -> None:
        block = {"object": "block", "id": "b1", "type": "unsupported_type", "has_children": False, "unsupported_type": {}}
        assert _block_to_text(block) == ""

    def test_callout_block(self) -> None:
        block = _make_block("callout", "Important note")
        assert _block_to_text(block) == "> Important note"


class TestNormalizePage:
    def test_stable_id_from_sha256(self) -> None:
        import hashlib
        page = _make_page(page_id=PAGE_ID)
        doc = normalize_page(page, CONNECTOR_ID, TENANT)
        expected_id = hashlib.sha256(PAGE_ID.encode()).hexdigest()[:16]
        assert doc.id == expected_id

    def test_title_extracted_from_title_property(self) -> None:
        page = _make_page(title="Test Title")
        doc = normalize_page(page, CONNECTOR_ID, TENANT)
        assert doc.title == "Test Title"

    def test_title_extracted_from_name_property(self) -> None:
        page = _make_page()
        page["properties"] = {
            "Name": {
                "type": "title",
                "title": [{"plain_text": "Name Title", "type": "text"}],
            }
        }
        doc = normalize_page(page, CONNECTOR_ID, TENANT)
        assert doc.title == "Name Title"

    def test_type_is_notion_page(self) -> None:
        page = _make_page()
        doc = normalize_page(page, CONNECTOR_ID, TENANT)
        assert doc.type == "notion_page"

    def test_url_in_content(self) -> None:
        page = _make_page(url="https://notion.so/test")
        doc = normalize_page(page, CONNECTOR_ID, TENANT)
        assert "https://notion.so/test" in doc.content

    def test_metadata_page_id(self) -> None:
        page = _make_page()
        doc = normalize_page(page, CONNECTOR_ID, TENANT)
        assert doc.metadata["page_id"] == PAGE_ID

    def test_metadata_source_is_notion(self) -> None:
        page = _make_page()
        doc = normalize_page(page, CONNECTOR_ID, TENANT)
        assert doc.metadata["source"] == "notion"

    def test_metadata_connector_and_tenant(self) -> None:
        page = _make_page()
        doc = normalize_page(page, CONNECTOR_ID, TENANT)
        assert doc.metadata["connector_id"] == CONNECTOR_ID
        assert doc.metadata["tenant_id"] == TENANT

    def test_metadata_object_type(self) -> None:
        page = _make_page()
        doc = normalize_page(page, CONNECTOR_ID, TENANT)
        assert doc.metadata["object_type"] == "page"

    def test_content_includes_blocks(self) -> None:
        page = _make_page()
        blocks = [_make_block("paragraph", "Block text here")]
        doc = normalize_page(page, CONNECTOR_ID, TENANT, content_blocks=blocks)
        assert "Block text here" in doc.content

    def test_archived_page_in_metadata(self) -> None:
        page = _make_page(archived=True)
        doc = normalize_page(page, CONNECTOR_ID, TENANT)
        assert doc.metadata["archived"] is True

    def test_same_page_id_always_same_doc_id(self) -> None:
        page = _make_page(page_id="stable-id-123")
        doc1 = normalize_page(page, CONNECTOR_ID, TENANT)
        doc2 = normalize_page(page, CONNECTOR_ID, TENANT)
        assert doc1.id == doc2.id

    def test_different_page_ids_different_doc_ids(self) -> None:
        page1 = _make_page(page_id="page-id-aaa")
        page2 = _make_page(page_id="page-id-bbb")
        doc1 = normalize_page(page1, CONNECTOR_ID, TENANT)
        doc2 = normalize_page(page2, CONNECTOR_ID, TENANT)
        assert doc1.id != doc2.id


class TestNormalizeDatabase:
    def test_stable_id_from_sha256(self) -> None:
        import hashlib
        database = _make_database(db_id=DATABASE_ID)
        doc = normalize_database(database, CONNECTOR_ID, TENANT)
        expected_id = hashlib.sha256(DATABASE_ID.encode()).hexdigest()[:16]
        assert doc.id == expected_id

    def test_title_from_top_level_array(self) -> None:
        database = _make_database(title="My DB")
        doc = normalize_database(database, CONNECTOR_ID, TENANT)
        assert doc.title == "My DB"

    def test_type_is_notion_database(self) -> None:
        database = _make_database()
        doc = normalize_database(database, CONNECTOR_ID, TENANT)
        assert doc.type == "notion_database"

    def test_metadata_database_id(self) -> None:
        database = _make_database()
        doc = normalize_database(database, CONNECTOR_ID, TENANT)
        assert doc.metadata["database_id"] == DATABASE_ID

    def test_metadata_source_is_notion(self) -> None:
        database = _make_database()
        doc = normalize_database(database, CONNECTOR_ID, TENANT)
        assert doc.metadata["source"] == "notion"

    def test_metadata_object_type(self) -> None:
        database = _make_database()
        doc = normalize_database(database, CONNECTOR_ID, TENANT)
        assert doc.metadata["object_type"] == "database"

    def test_property_names_in_metadata(self) -> None:
        database = _make_database()
        doc = normalize_database(database, CONNECTOR_ID, TENANT)
        assert "Name" in doc.metadata["property_names"]
        assert "Status" in doc.metadata["property_names"]
        assert "Due" in doc.metadata["property_names"]

    def test_property_names_in_content(self) -> None:
        database = _make_database()
        doc = normalize_database(database, CONNECTOR_ID, TENANT)
        assert "Name" in doc.content

    def test_url_in_content(self) -> None:
        database = _make_database(url="https://notion.so/mydb")
        doc = normalize_database(database, CONNECTOR_ID, TENANT)
        assert "https://notion.so/mydb" in doc.content

    def test_empty_title_array_gives_untitled(self) -> None:
        database = _make_database()
        database["title"] = []
        doc = normalize_database(database, CONNECTOR_ID, TENANT)
        assert doc.title == "Untitled Database"


# ── 4. with_retry ─────────────────────────────────────────────────────────────

class TestWithRetry:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        mock_fn = AsyncMock(return_value={"ok": True})
        result = await with_retry(mock_fn, max_attempts=3)
        assert result == {"ok": True}
        assert mock_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_notion_error(self) -> None:
        calls = 0

        async def flaky() -> Dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise NotionError("temporary error")
            return {"ok": True}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(flaky, max_attempts=3)
        assert result == {"ok": True}
        assert calls == 3

    @pytest.mark.asyncio
    async def test_does_not_retry_on_auth_error(self) -> None:
        mock_fn = AsyncMock(side_effect=NotionAuthError("invalid token"))
        with pytest.raises(NotionAuthError):
            await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_does_not_retry_on_not_found_error(self) -> None:
        mock_fn = AsyncMock(side_effect=NotionNotFoundError("page not found"))
        with pytest.raises(NotionNotFoundError):
            await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_raises_after_max_attempts(self) -> None:
        mock_fn = AsyncMock(side_effect=NotionError("always fails"))
        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(NotionError, match="always fails"):
                await with_retry(mock_fn, max_attempts=3)
        assert mock_fn.call_count == 3

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit_error(self) -> None:
        calls = 0

        async def rate_limited() -> Dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise NotionRateLimitError("429")
            return {"data": "ok"}

        with patch("helpers.utils.asyncio.sleep", new_callable=AsyncMock):
            result = await with_retry(rate_limited, max_attempts=3)
        assert result == {"data": "ok"}
        assert calls == 2


# ── 5. HTTP Client ────────────────────────────────────────────────────────────

class TestNotionHTTPClientHeaders:
    def test_auth_header_uses_bearer(self) -> None:
        client = NotionHTTPClient()
        headers = client._auth_headers(TOKEN)
        assert headers["Authorization"] == f"Bearer {TOKEN}"

    def test_notion_version_header_present(self) -> None:
        client = NotionHTTPClient()
        headers = client._auth_headers(TOKEN)
        assert headers["Notion-Version"] == "2022-06-28"

    def test_content_type_header_present(self) -> None:
        client = NotionHTTPClient()
        headers = client._auth_headers(TOKEN)
        assert headers["Content-Type"] == "application/json"


class TestRaiseForStatus:
    def test_200_returns_data(self) -> None:
        client = NotionHTTPClient()
        data = {"object": "page", "id": "123"}
        result = client._raise_for_status(200, data, "test")
        assert result == data

    def test_401_raises_auth_error(self) -> None:
        client = NotionHTTPClient()
        with pytest.raises(NotionAuthError):
            client._raise_for_status(401, {"message": "unauthorized"}, "test")

    def test_403_raises_auth_error(self) -> None:
        client = NotionHTTPClient()
        with pytest.raises(NotionAuthError):
            client._raise_for_status(403, {"message": "forbidden"}, "test")

    def test_404_raises_not_found(self) -> None:
        client = NotionHTTPClient()
        with pytest.raises(NotionNotFoundError):
            client._raise_for_status(404, {"message": "not found"}, "test")

    def test_429_raises_rate_limit(self) -> None:
        client = NotionHTTPClient()
        with pytest.raises(NotionRateLimitError):
            client._raise_for_status(429, {}, "test")

    def test_500_raises_network_error(self) -> None:
        client = NotionHTTPClient()
        with pytest.raises(NotionNetworkError):
            client._raise_for_status(500, {"message": "internal server error"}, "test")

    def test_400_raises_notion_error(self) -> None:
        client = NotionHTTPClient()
        with pytest.raises(NotionError):
            client._raise_for_status(400, {"message": "bad request"}, "test")

    def test_check_response_alias_works(self) -> None:
        client = NotionHTTPClient()
        data = {"id": "x"}
        result = client._check_response(200, data, "ctx")
        assert result == data


class TestHTTPClientGetBotUser:
    @pytest.mark.asyncio
    async def test_get_bot_user_success(self) -> None:
        client = NotionHTTPClient()
        bot_response = {"object": "user", "id": "bot-id-123", "type": "bot", "name": "My Integration Bot"}
        mock_session = _mock_session_get(bot_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_bot_user(TOKEN)
        assert result["name"] == "My Integration Bot"

    @pytest.mark.asyncio
    async def test_get_bot_user_includes_notion_version_header(self) -> None:
        client = NotionHTTPClient()
        bot_response = {"object": "user", "id": "x", "name": "Bot"}
        mock_session = _mock_session_get(bot_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            await client.get_bot_user(TOKEN)

        call_kwargs = mock_session.get.call_args
        headers = call_kwargs[1]["headers"] if call_kwargs[1] else call_kwargs[0][1]
        assert headers["Notion-Version"] == "2022-06-28"

    @pytest.mark.asyncio
    async def test_get_bot_user_401_raises_auth_error(self) -> None:
        client = NotionHTTPClient()
        mock_session = _mock_session_get({"message": "API token is invalid."}, status=401)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(NotionAuthError):
                await client.get_bot_user(TOKEN)

    @pytest.mark.asyncio
    async def test_get_user_me_alias_works(self) -> None:
        client = NotionHTTPClient()
        bot_response = {"object": "user", "id": "bot-id", "name": "Bot"}
        mock_session = _mock_session_get(bot_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_user_me(TOKEN)
        assert result["name"] == "Bot"

    @pytest.mark.asyncio
    async def test_get_bot_user_network_error_raises(self) -> None:
        import aiohttp as _aiohttp
        client = NotionHTTPClient()

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=_aiohttp.ClientError("connection refused"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(NotionNetworkError):
                await client.get_bot_user(TOKEN)


class TestHTTPClientListUsers:
    @pytest.mark.asyncio
    async def test_list_users_success(self) -> None:
        client = NotionHTTPClient()
        users_response = {
            "object": "list",
            "results": [
                {"object": "user", "id": "user-1", "name": "Alice"},
                {"object": "user", "id": "user-2", "name": "Bob"},
            ],
            "has_more": False,
            "next_cursor": None,
        }
        mock_session = _mock_session_get(users_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.list_users(TOKEN)
        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_list_users_with_cursor(self) -> None:
        client = NotionHTTPClient()
        users_response = {"object": "list", "results": [], "has_more": False}
        mock_session = _mock_session_get(users_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.list_users(TOKEN, start_cursor="cursor-abc")
        assert result["has_more"] is False

    @pytest.mark.asyncio
    async def test_list_users_notion_version_header(self) -> None:
        client = NotionHTTPClient()
        users_response = {"object": "list", "results": [], "has_more": False}
        mock_session = _mock_session_get(users_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            await client.list_users(TOKEN)

        call_kwargs = mock_session.get.call_args
        headers = call_kwargs[1]["headers"] if call_kwargs[1] else call_kwargs[0][1]
        assert headers["Notion-Version"] == "2022-06-28"


class TestHTTPClientSearch:
    @pytest.mark.asyncio
    async def test_search_success(self) -> None:
        client = NotionHTTPClient()
        search_response = _make_search_response([_make_page()])
        mock_session = _mock_session_post(search_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.search(TOKEN, query="test")
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_search_with_page_filter(self) -> None:
        client = NotionHTTPClient()
        search_response = _make_search_response([])
        mock_session = _mock_session_post(search_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.search(TOKEN, filter_type="page")
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_search_with_database_filter(self) -> None:
        client = NotionHTTPClient()
        search_response = _make_search_response([_make_database()])
        mock_session = _mock_session_post(search_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.search(TOKEN, filter_type="database")
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_search_notion_version_header(self) -> None:
        client = NotionHTTPClient()
        search_response = _make_search_response([])
        mock_session = _mock_session_post(search_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            await client.search(TOKEN)

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs[1]["headers"]
        assert headers["Notion-Version"] == "2022-06-28"


class TestHTTPClientGetPage:
    @pytest.mark.asyncio
    async def test_get_page_success(self) -> None:
        client = NotionHTTPClient()
        page = _make_page()
        mock_session = _mock_session_get(page)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_page(TOKEN, PAGE_ID)
        assert result["id"] == PAGE_ID

    @pytest.mark.asyncio
    async def test_get_page_404_raises_not_found(self) -> None:
        client = NotionHTTPClient()
        mock_session = _mock_session_get({"message": "Could not find page."}, status=404)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(NotionNotFoundError):
                await client.get_page(TOKEN, "nonexistent-page-id")

    @pytest.mark.asyncio
    async def test_get_page_notion_version_header(self) -> None:
        client = NotionHTTPClient()
        mock_session = _mock_session_get(_make_page())

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            await client.get_page(TOKEN, PAGE_ID)

        call_kwargs = mock_session.get.call_args
        headers = call_kwargs[1]["headers"] if call_kwargs[1] else call_kwargs[0][1]
        assert headers["Notion-Version"] == "2022-06-28"


class TestHTTPClientGetPageContent:
    @pytest.mark.asyncio
    async def test_get_page_content_success(self) -> None:
        client = NotionHTTPClient()
        block_response = _make_block_response([_make_block("paragraph", "Block content")])
        mock_session = _mock_session_get(block_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_page_content(TOKEN, PAGE_ID)
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_get_page_content_with_cursor(self) -> None:
        client = NotionHTTPClient()
        block_response = _make_block_response([])
        mock_session = _mock_session_get(block_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_page_content(TOKEN, PAGE_ID, start_cursor="cur-1")
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_get_block_children_notion_version_header(self) -> None:
        client = NotionHTTPClient()
        block_response = _make_block_response([])
        mock_session = _mock_session_get(block_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            await client.get_block_children(TOKEN, PAGE_ID)

        call_kwargs = mock_session.get.call_args
        headers = call_kwargs[1]["headers"] if call_kwargs[1] else call_kwargs[0][1]
        assert headers["Notion-Version"] == "2022-06-28"


class TestHTTPClientGetDatabase:
    @pytest.mark.asyncio
    async def test_get_database_success(self) -> None:
        client = NotionHTTPClient()
        database = _make_database()
        mock_session = _mock_session_get(database)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.get_database(TOKEN, DATABASE_ID)
        assert result["id"] == DATABASE_ID

    @pytest.mark.asyncio
    async def test_get_database_404_raises_not_found(self) -> None:
        client = NotionHTTPClient()
        mock_session = _mock_session_get({"message": "Not found"}, status=404)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(NotionNotFoundError):
                await client.get_database(TOKEN, "nonexistent-db")


class TestHTTPClientQueryDatabase:
    @pytest.mark.asyncio
    async def test_query_database_success(self) -> None:
        client = NotionHTTPClient()
        query_response = {"object": "list", "results": [_make_page()], "has_more": False}
        mock_session = _mock_session_post(query_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.query_database(TOKEN, DATABASE_ID)
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_query_database_with_sorts(self) -> None:
        client = NotionHTTPClient()
        query_response = {"object": "list", "results": [], "has_more": False}
        mock_session = _mock_session_post(query_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.query_database(
                TOKEN,
                DATABASE_ID,
                sorts=[{"property": "Name", "direction": "ascending"}],
            )
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_query_database_notion_version_header(self) -> None:
        client = NotionHTTPClient()
        query_response = {"object": "list", "results": [], "has_more": False}
        mock_session = _mock_session_post(query_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            await client.query_database(TOKEN, DATABASE_ID)

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs[1]["headers"]
        assert headers["Notion-Version"] == "2022-06-28"


class TestHTTPClientListDatabases:
    @pytest.mark.asyncio
    async def test_list_databases_uses_search_with_database_filter(self) -> None:
        client = NotionHTTPClient()
        db_response = _make_search_response([_make_database()])
        mock_session = _mock_session_post(db_response)

        with patch("client.http_client.aiohttp.ClientSession", return_value=mock_session):
            result = await client.list_databases(TOKEN)
        assert len(result["results"]) == 1


# ── 6. Connector init & config ────────────────────────────────────────────────

class TestNotionConnectorInit:
    def test_default_init(self) -> None:
        connector = NotionConnector()
        assert connector.tenant_id == ""
        assert connector.connector_id == ""
        assert connector.config == {}

    def test_init_with_api_key_config(self) -> None:
        connector = _make_connector()
        assert connector.tenant_id == TENANT
        assert connector.connector_id == CONNECTOR_ID
        assert connector.config["api_key"] == TOKEN

    def test_connector_type(self) -> None:
        assert NotionConnector.CONNECTOR_TYPE == "notion"

    def test_connector_name(self) -> None:
        assert NotionConnector.CONNECTOR_NAME == "Notion"

    def test_auth_type(self) -> None:
        assert NotionConnector.AUTH_TYPE == "api_key"

    def test_required_config_keys_contains_api_key(self) -> None:
        assert "api_key" in NotionConnector.REQUIRED_CONFIG_KEYS

    def test_get_token_returns_api_key(self) -> None:
        connector = _make_connector()
        assert connector._get_token() == TOKEN

    def test_get_token_returns_empty_when_missing(self) -> None:
        connector = NotionConnector(config={})
        assert connector._get_token() == ""

    def test_get_token_falls_back_to_integration_token(self) -> None:
        connector = NotionConnector(config={"integration_token": "secret_fallback"})
        assert connector._get_token() == "secret_fallback"

    def test_ensure_client_returns_http_client(self) -> None:
        connector = _make_connector()
        client = connector._ensure_client()
        assert isinstance(client, NotionHTTPClient)
        assert connector._ensure_client() is client


# ── 7. Install ────────────────────────────────────────────────────────────────

class TestNotionConnectorInstall:
    @pytest.mark.asyncio
    async def test_install_with_api_key_returns_healthy(self) -> None:
        connector = _make_connector()
        result = await connector.install()
        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert result.connector_id == CONNECTOR_ID

    @pytest.mark.asyncio
    async def test_install_without_api_key_returns_offline(self) -> None:
        connector = NotionConnector(
            tenant_id=TENANT, connector_id=CONNECTOR_ID, config={}
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_with_empty_api_key_returns_offline(self) -> None:
        connector = NotionConnector(
            tenant_id=TENANT, connector_id=CONNECTOR_ID, config={"api_key": ""}
        )
        result = await connector.install()
        assert result.health == ConnectorHealth.OFFLINE
        assert result.auth_status == AuthStatus.MISSING_CREDENTIALS

    @pytest.mark.asyncio
    async def test_install_missing_message_contains_api_key(self) -> None:
        connector = NotionConnector(config={})
        result = await connector.install()
        assert "api_key" in result.message

    @pytest.mark.asyncio
    async def test_install_returns_install_result_type(self) -> None:
        connector = _make_connector()
        result = await connector.install()
        assert isinstance(result, InstallResult)


# ── 8. Health check ───────────────────────────────────────────────────────────

class TestNotionConnectorHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_success(self) -> None:
        connector = _make_connector()
        bot_data = {"object": "user", "id": "bot-id", "type": "bot", "name": "Shielva Bot"}

        with patch.object(
            connector._ensure_client(), "get_bot_user",
            new_callable=AsyncMock, return_value=bot_data,
        ):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.HEALTHY
        assert result.auth_status == AuthStatus.CONNECTED
        assert "Shielva Bot" in result.message

    @pytest.mark.asyncio
    async def test_health_check_uses_id_when_name_missing(self) -> None:
        connector = _make_connector()
        bot_data = {"object": "user", "id": "bot-id-xyz", "type": "bot"}

        with patch.object(
            connector._ensure_client(), "get_bot_user",
            new_callable=AsyncMock, return_value=bot_data,
        ):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.HEALTHY
        assert "bot-id-xyz" in result.message

    @pytest.mark.asyncio
    async def test_health_check_auth_error_returns_degraded(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector._ensure_client(), "get_bot_user",
            new_callable=AsyncMock, side_effect=NotionAuthError("invalid token"),
        ):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.INVALID_CREDENTIALS

    @pytest.mark.asyncio
    async def test_health_check_network_error_returns_degraded(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector._ensure_client(), "get_bot_user",
            new_callable=AsyncMock, side_effect=NotionNetworkError("timeout"),
        ):
            result = await connector.health_check()

        assert result.health == ConnectorHealth.DEGRADED
        assert result.auth_status == AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_health_check_returns_health_check_result_type(self) -> None:
        connector = _make_connector()
        bot_data = {"object": "user", "id": "x", "name": "Bot"}

        with patch.object(
            connector._ensure_client(), "get_bot_user",
            new_callable=AsyncMock, return_value=bot_data,
        ):
            result = await connector.health_check()
        assert isinstance(result, HealthCheckResult)


# ── 9. list_pages ─────────────────────────────────────────────────────────────

class TestNotionConnectorListPages:
    @pytest.mark.asyncio
    async def test_list_pages_returns_pages(self) -> None:
        connector = _make_connector()
        page = _make_page()
        search_resp = _make_search_response([page])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            results = await connector.list_pages()

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_list_pages_paginated(self) -> None:
        connector = _make_connector()
        page1 = _make_page(page_id="page-aaa")
        page2 = _make_page(page_id="page-bbb")
        call_count = 0

        async def mock_search(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_search_response([page1], has_more=True, next_cursor="cursor-1")
            return _make_search_response([page2], has_more=False)

        with patch.object(connector._ensure_client(), "search", side_effect=mock_search):
            results = await connector.list_pages()

        assert len(results) == 2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_list_pages_with_query(self) -> None:
        connector = _make_connector()
        search_resp = _make_search_response([])
        mock_search = AsyncMock(return_value=search_resp)

        with patch.object(connector._ensure_client(), "search", mock_search):
            await connector.list_pages(query="meeting notes")

        assert mock_search.called

    @pytest.mark.asyncio
    async def test_list_pages_empty(self) -> None:
        connector = _make_connector()
        search_resp = _make_search_response([])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            results = await connector.list_pages()

        assert results == []


# ── 10. list_databases ────────────────────────────────────────────────────────

class TestNotionConnectorListDatabases:
    @pytest.mark.asyncio
    async def test_list_databases_returns_databases(self) -> None:
        connector = _make_connector()
        database = _make_database()
        search_resp = _make_search_response([database])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            results = await connector.list_databases()

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_list_databases_paginated(self) -> None:
        connector = _make_connector()
        db1 = _make_database(db_id="db-aaa")
        db2 = _make_database(db_id="db-bbb")
        call_count = 0

        async def mock_search(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_search_response([db1], has_more=True, next_cursor="cursor-1")
            return _make_search_response([db2], has_more=False)

        with patch.object(connector._ensure_client(), "search", side_effect=mock_search):
            results = await connector.list_databases()

        assert len(results) == 2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_list_databases_empty(self) -> None:
        connector = _make_connector()
        search_resp = _make_search_response([])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            results = await connector.list_databases()

        assert results == []


# ── 11. get_page ──────────────────────────────────────────────────────────────

class TestNotionConnectorGetPage:
    @pytest.mark.asyncio
    async def test_get_page_success(self) -> None:
        connector = _make_connector()
        page = _make_page()

        with patch.object(connector._ensure_client(), "get_page", AsyncMock(return_value=page)):
            result = await connector.get_page(PAGE_ID)

        assert result["id"] == PAGE_ID

    @pytest.mark.asyncio
    async def test_get_page_not_found_raises(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector._ensure_client(), "get_page",
            new_callable=AsyncMock, side_effect=NotionNotFoundError("page not found"),
        ):
            with pytest.raises(NotionNotFoundError):
                await connector.get_page("nonexistent-id")


# ── 12. get_database ──────────────────────────────────────────────────────────

class TestNotionConnectorGetDatabase:
    @pytest.mark.asyncio
    async def test_get_database_success(self) -> None:
        connector = _make_connector()
        database = _make_database()

        with patch.object(connector._ensure_client(), "get_database", AsyncMock(return_value=database)):
            result = await connector.get_database(DATABASE_ID)

        assert result["id"] == DATABASE_ID

    @pytest.mark.asyncio
    async def test_get_database_not_found_raises(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector._ensure_client(), "get_database",
            new_callable=AsyncMock, side_effect=NotionNotFoundError("database not found"),
        ):
            with pytest.raises(NotionNotFoundError):
                await connector.get_database("nonexistent-db-id")


# ── 13. query_database ────────────────────────────────────────────────────────

class TestNotionConnectorQueryDatabase:
    @pytest.mark.asyncio
    async def test_query_database_returns_pages(self) -> None:
        connector = _make_connector()
        page = _make_page()
        query_resp = {"object": "list", "results": [page], "has_more": False}

        with patch.object(connector._ensure_client(), "query_database", AsyncMock(return_value=query_resp)):
            results = await connector.query_database(DATABASE_ID)

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_query_database_with_filter(self) -> None:
        connector = _make_connector()
        query_resp = {"object": "list", "results": [], "has_more": False}
        mock_query = AsyncMock(return_value=query_resp)

        with patch.object(connector._ensure_client(), "query_database", mock_query):
            await connector.query_database(
                DATABASE_ID,
                filter={"property": "Status", "select": {"equals": "Done"}},
            )

        assert mock_query.called

    @pytest.mark.asyncio
    async def test_query_database_with_sorts(self) -> None:
        connector = _make_connector()
        query_resp = {"object": "list", "results": [], "has_more": False}
        mock_query = AsyncMock(return_value=query_resp)

        with patch.object(connector._ensure_client(), "query_database", mock_query):
            await connector.query_database(
                DATABASE_ID,
                sorts=[{"property": "Name", "direction": "ascending"}],
            )

        assert mock_query.called

    @pytest.mark.asyncio
    async def test_query_database_paginated(self) -> None:
        connector = _make_connector()
        page1 = _make_page(page_id="row-aaa")
        page2 = _make_page(page_id="row-bbb")
        call_count = 0

        async def mock_query(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"object": "list", "results": [page1], "has_more": True, "next_cursor": "cur-1"}
            return {"object": "list", "results": [page2], "has_more": False}

        with patch.object(connector._ensure_client(), "query_database", side_effect=mock_query):
            results = await connector.query_database(DATABASE_ID)

        assert len(results) == 2
        assert call_count == 2


# ── 14. get_page_blocks ───────────────────────────────────────────────────────

class TestNotionConnectorGetPageBlocks:
    @pytest.mark.asyncio
    async def test_get_page_blocks_returns_blocks(self) -> None:
        connector = _make_connector()
        block = _make_block("paragraph", "Content here", has_children=False)
        block_resp = _make_block_response([block])

        with patch.object(connector._ensure_client(), "get_block_children", AsyncMock(return_value=block_resp)):
            blocks = await connector.get_page_blocks(PAGE_ID)

        assert len(blocks) == 1
        assert blocks[0]["type"] == "paragraph"

    @pytest.mark.asyncio
    async def test_get_page_blocks_recursive(self) -> None:
        connector = _make_connector()
        parent_block = _make_block("paragraph", "Parent", has_children=True, block_id="parent-block")
        child_block = _make_block("paragraph", "Child", has_children=False, block_id="child-block")
        call_count = 0

        async def mock_get_children(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_block_response([parent_block])
            return _make_block_response([child_block])

        with patch.object(connector._ensure_client(), "get_block_children", side_effect=mock_get_children):
            blocks = await connector.get_page_blocks(PAGE_ID)

        assert call_count >= 2
        assert len(blocks) == 2

    @pytest.mark.asyncio
    async def test_get_page_content_alias_works(self) -> None:
        connector = _make_connector()
        block = _make_block("paragraph", "Text")
        block_resp = _make_block_response([block])

        with patch.object(connector._ensure_client(), "get_block_children", AsyncMock(return_value=block_resp)):
            blocks = await connector.get_page_content(PAGE_ID)

        assert len(blocks) == 1

    @pytest.mark.asyncio
    async def test_get_page_blocks_paginated(self) -> None:
        connector = _make_connector()
        block1 = _make_block("paragraph", "Block 1", block_id="b1")
        block2 = _make_block("paragraph", "Block 2", block_id="b2")
        call_count = 0

        async def mock_get_children(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_block_response([block1], has_more=True, next_cursor="cur-1")
            return _make_block_response([block2], has_more=False)

        with patch.object(connector._ensure_client(), "get_block_children", side_effect=mock_get_children):
            blocks = await connector.get_page_blocks(PAGE_ID)

        assert len(blocks) == 2


# ── 15. Sync ──────────────────────────────────────────────────────────────────

class TestNotionConnectorSync:
    @pytest.mark.asyncio
    async def test_sync_pages_and_databases(self) -> None:
        connector = _make_connector()
        page = _make_page()
        database = _make_database()
        search_resp = _make_search_response([page, database])
        block_resp = _make_block_response([])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            with patch.object(connector._ensure_client(), "get_block_children", AsyncMock(return_value=block_resp)):
                result = await connector.sync()

        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 2
        assert result.documents_synced == 2
        assert result.documents_failed == 0

    @pytest.mark.asyncio
    async def test_sync_empty_workspace(self) -> None:
        connector = _make_connector()
        search_resp = _make_search_response([])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            result = await connector.sync()

        assert result.status == SyncStatus.COMPLETED
        assert result.documents_found == 0

    @pytest.mark.asyncio
    async def test_sync_unknown_object_skipped(self) -> None:
        connector = _make_connector()
        unknown_obj = {"object": "unknown_type", "id": "some-id"}
        search_resp = _make_search_response([unknown_obj])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            result = await connector.sync()

        assert result.documents_found == 0
        assert result.documents_synced == 0

    @pytest.mark.asyncio
    async def test_sync_partial_on_object_failure(self) -> None:
        connector = _make_connector()
        page = _make_page()
        bad_page = {"object": "page", "id": "bad-page"}
        search_resp = _make_search_response([page, bad_page])
        block_resp = _make_block_response([])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            with patch.object(connector._ensure_client(), "get_block_children", AsyncMock(return_value=block_resp)):
                with patch("connector.normalize_page", side_effect=[
                    normalize_page(page, CONNECTOR_ID, TENANT),
                    Exception("normalization failed"),
                ]):
                    result = await connector.sync()

        assert result.status == SyncStatus.PARTIAL
        assert result.documents_synced == 1
        assert result.documents_failed == 1

    @pytest.mark.asyncio
    async def test_sync_failed_on_search_error(self) -> None:
        connector = _make_connector()

        with patch.object(
            connector._ensure_client(), "search",
            new_callable=AsyncMock, side_effect=NotionAuthError("invalid token"),
        ):
            result = await connector.sync()

        assert result.status == SyncStatus.FAILED

    @pytest.mark.asyncio
    async def test_sync_message_contains_counts(self) -> None:
        connector = _make_connector()
        page = _make_page()
        search_resp = _make_search_response([page])
        block_resp = _make_block_response([])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            with patch.object(connector._ensure_client(), "get_block_children", AsyncMock(return_value=block_resp)):
                result = await connector.sync()

        assert "1" in result.message

    @pytest.mark.asyncio
    async def test_sync_returns_sync_result_type(self) -> None:
        connector = _make_connector()
        search_resp = _make_search_response([])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            result = await connector.sync()

        assert isinstance(result, SyncResult)

    @pytest.mark.asyncio
    async def test_sync_accepts_kwargs(self) -> None:
        connector = _make_connector()
        search_resp = _make_search_response([])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            result = await connector.sync(full=True, kb_id="kb-123")

        assert result.status == SyncStatus.COMPLETED


# ── 16. Lifecycle ─────────────────────────────────────────────────────────────

class TestNotionConnectorLifecycle:
    @pytest.mark.asyncio
    async def test_aclose_clears_client(self) -> None:
        connector = _make_connector()
        _ = connector._ensure_client()
        assert connector._http_client is not None
        await connector.aclose()
        assert connector._http_client is None

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        async with NotionConnector(config={"api_key": TOKEN}) as connector:
            assert isinstance(connector, NotionConnector)
        assert connector._http_client is None

    def test_ensure_client_same_instance_on_second_call(self) -> None:
        connector = _make_connector()
        client1 = connector._ensure_client()
        client2 = connector._ensure_client()
        assert client1 is client2

    @pytest.mark.asyncio
    async def test_search_method(self) -> None:
        connector = _make_connector()
        page = _make_page()
        search_resp = _make_search_response([page, _make_database()])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            results = await connector.search(query="test")

        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_search_with_filter_type(self) -> None:
        connector = _make_connector()
        search_resp = _make_search_response([])

        with patch.object(connector._ensure_client(), "search", AsyncMock(return_value=search_resp)):
            results = await connector.search(filter_type="page")

        assert results == []
