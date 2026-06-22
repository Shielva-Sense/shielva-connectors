"""Bitbucket Cloud connector — orchestration only.

All HTTP calls → ``client/http_client.py``
All normalization → ``helpers/normalizer.py``
All utilities → ``helpers/utils.py``

Auth: OAuth 2.0 Authorization Code Grant. Access tokens expire after 2 hours;
refresh tokens are minted at first authorize. The BaseConnector ``ensure_token()``
machinery calls ``on_token_refresh()`` ahead of expiry. A mid-flight 401 is
handled in-client (one-shot refresh + replay) via the ``token_refresh``
callback wired in ``__init__``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import structlog
from shared.base_connector import (
    AuthStatus,
    BaseConnector,
    ConnectorHealth,
    ConnectorStatus,
    NormalizedDocument,
    RefreshError,
    SyncResult,
    SyncStatus,
    TokenInfo,
)

from client.http_client import BitbucketHTTPClient
from exceptions import (
    BitbucketAuthError,
    BitbucketError,
    BitbucketNetworkError,
    BitbucketNotFound,
)
from helpers.normalizer import (
    normalize_issue,
    normalize_pull_request,
    normalize_repository,
)
from helpers.utils import with_retry

logger = structlog.get_logger(__name__)

AUTH_URI = "https://bitbucket.org/site/oauth2/authorize"
TOKEN_URI = "https://bitbucket.org/site/oauth2/access_token"
_BITBUCKET_BASE = "https://api.bitbucket.org/2.0"

DEFAULT_SCOPES = (
    "account repository repository:write "
    "pullrequest pullrequest:write issue issue:write"
)


class BitbucketConnector(BaseConnector):
    """Shielva connector for the Bitbucket Cloud REST API."""

    CONNECTOR_TYPE = "bitbucket"
    CONNECTOR_NAME = "Bitbucket"
    AUTH_TYPE = "oauth2_code"

    # Provider-wide OAuth2 endpoints (class constants — BaseConnector reads these).
    AUTH_URI = AUTH_URI
    TOKEN_URI = TOKEN_URI

    REQUIRED_SCOPES: List[str] = [
        "account",
        "repository",
        "repository:write",
        "pullrequest",
        "pullrequest:write",
        "issue",
        "issue:write",
    ]

    # Public — gateway / installer validates against this list.
    REQUIRED_CONFIG_KEYS: List[str] = ["client_id", "client_secret"]

    # OCP — HTTP status → (ConnectorHealth, AuthStatus) classification.
    _STATUS_MAP: Dict[int, Any] = {
        401: ("DEGRADED", "TOKEN_EXPIRED"),
        403: ("DEGRADED", "AUTHENTICATED"),
        429: ("DEGRADED", "CONNECTED"),
    }

    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None,
    ) -> None:
        super().__init__(tenant_id, connector_id, config)
        # Per-tenant user-supplied credentials — NEVER hardcoded.
        self.client_id: str = self.config.get("client_id", "")
        self.client_secret: str = self.config.get("client_secret", "")
        self.scopes: str = self.config.get("scopes", DEFAULT_SCOPES)
        self.auth_url: str = self.config.get("auth_url", AUTH_URI)
        self.token_url: str = self.config.get("token_url", TOKEN_URI)
        self.base_url: str = self.config.get("base_url", _BITBUCKET_BASE)
        # Gateway injects redirect_uri before calling authorize().
        self.redirect_uri: str = self.config.get("redirect_uri", "")
        self.rate_limit_per_min: Any = self.config.get("rate_limit_per_min", 60)

        self.http_client = BitbucketHTTPClient(
            base_url=self.base_url or _BITBUCKET_BASE,
            token_refresh=self._refresh_for_http_client,
        )

    # ── Internal helpers ────────────────────────────────────────────────────

    async def _get_valid_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        token_info = await self.ensure_token()
        return token_info.access_token

    async def _refresh_for_http_client(self) -> str:
        """Callback invoked by BitbucketHTTPClient when it sees a 401.

        Refreshes the OAuth access token and returns the new access_token
        string so the in-flight request can be replayed.
        """
        new_token = await self.on_token_refresh()
        await self.set_token(new_token)
        return new_token.access_token

    def _classify_failure(self, exc: Exception) -> ConnectorStatus:
        """OCP — map any exception to a ConnectorStatus via _STATUS_MAP."""
        status = getattr(exc, "status_code", 0)
        health_name, auth_name = self._STATUS_MAP.get(status, ("DEGRADED", "FAILED"))
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth[health_name],
            auth_status=AuthStatus[auth_name],
            message=str(exc),
        )

    async def on_token_refresh(self) -> TokenInfo:
        """Refresh the OAuth2 access token using the stored refresh token."""
        if not self._token_info or not self._token_info.refresh_token:
            raise RefreshError("No refresh token available")

        client_id = self.config.get("client_id", "") or self.client_id
        client_secret = self.config.get("client_secret", "") or self.client_secret
        token_uri = self.config.get("token_url") or TOKEN_URI

        stored_token = self._token_info.refresh_token
        data = await self.http_client.post_form_data(
            url=token_uri,
            payload={
                "grant_type": "refresh_token",
                "refresh_token": stored_token,
            },
            basic_auth=(client_id, client_secret),
            context="on_token_refresh",
        )

        expires_in = int(data.get("expires_in", 7200))
        scope_str = data.get("scopes") or data.get("scope") or ""
        new_scopes = scope_str.split() if scope_str else list(self._token_info.scopes)
        # Bitbucket may omit refresh_token on refresh responses — preserve the old one.
        new_refresh = data.get("refresh_token") or stored_token
        return TokenInfo(
            access_token=data["access_token"],
            refresh_token=new_refresh,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=new_scopes,
        )

    # ── BaseConnector abstract surface ─────────────────────────────────────

    async def install(self) -> ConnectorStatus:
        """Validate install-time credentials and persist them.

        ``install()`` does NOT call the Bitbucket API — the OAuth code exchange
        happens in ``authorize()``. We only check ``client_id`` + ``client_secret``
        are non-empty so the gateway can render the authorization URL.
        """
        missing = [k for k in self.REQUIRED_CONFIG_KEYS if not self.config.get(k)]
        if missing:
            logger.warning(
                "bitbucket.install.missing_credentials",
                connector_id=self.connector_id,
                missing=missing,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.MISSING_CREDENTIALS,
                message=f"Missing required config keys: {', '.join(missing)}",
            )

        await self.save_config({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scopes": self.scopes,
            "auth_url": self.auth_url,
            "token_url": self.token_url,
            "base_url": self.base_url,
            "redirect_uri": self.config.get("redirect_uri", ""),
            "rate_limit_per_min": self.rate_limit_per_min,
        })
        logger.info("bitbucket.install.ok", connector_id=self.connector_id)
        return ConnectorStatus(
            connector_id=self.connector_id,
            health=ConnectorHealth.HEALTHY,
            auth_status=AuthStatus.PENDING,
            message="Bitbucket connector installed — complete OAuth to connect",
            metadata={"requires_oauth_redirect": True},
        )

    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Exchange the OAuth ``auth_code`` for access + refresh tokens."""
        if not auth_code:
            raise BitbucketAuthError("authorize() requires a non-empty auth_code")

        client_id = self.config.get("client_id", "") or self.client_id
        client_secret = self.config.get("client_secret", "") or self.client_secret
        token_uri = self.config.get("token_url") or TOKEN_URI
        redirect_uri = self.config.get("redirect_uri", "") or self.redirect_uri

        payload: Dict[str, str] = {
            "grant_type": "authorization_code",
            "code": auth_code,
        }
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri

        data = await self.http_client.post_form_data(
            url=token_uri,
            payload=payload,
            basic_auth=(client_id, client_secret),
            context="authorize",
        )

        expires_in = int(data.get("expires_in", 7200))
        scope_str = data.get("scopes") or data.get("scope") or ""
        scopes = scope_str.split() if scope_str else list(self.REQUIRED_SCOPES)
        token_info = TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            token_type=data.get("token_type", "Bearer"),
            scopes=scopes,
        )
        await self.set_token(token_info)
        logger.info(
            "bitbucket.authorize.ok",
            connector_id=self.connector_id,
            has_refresh=bool(token_info.refresh_token),
        )
        return token_info

    async def health_check(self) -> ConnectorStatus:
        """Verify Bitbucket API connectivity by calling ``GET /user``."""
        try:
            access_token = await self._get_valid_token()
            await with_retry(
                lambda: self.http_client.get_user(access_token),
                max_retries=2,
            )
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.HEALTHY,
                auth_status=AuthStatus.CONNECTED,
                message="Bitbucket API reachable",
            )
        except BitbucketAuthError:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.DEGRADED,
                auth_status=AuthStatus.TOKEN_EXPIRED,
                message="Token expired — re-authorize the connector",
            )
        except RefreshError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.EXPIRED,
                message=str(exc),
            )
        except BitbucketNetworkError as exc:
            return ConnectorStatus(
                connector_id=self.connector_id,
                health=ConnectorHealth.OFFLINE,
                auth_status=AuthStatus.CONNECTED,
                message=f"Bitbucket network error: {exc}",
            )
        except BitbucketError as exc:
            return self._classify_failure(exc)

    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        kb_id: str = None,
        webhook_url: str = None,
    ) -> SyncResult:
        """Sync Bitbucket repositories' pull requests into the knowledge base.

        For every workspace the principal can see, iterate repositories and
        push open PRs as NormalizedDocuments. Conservative baseline —
        incremental sync is layered in later via checkpoint metadata.
        """
        _ = since, full  # documented above
        access_token = await self._get_valid_token()

        documents_found = 0
        documents_synced = 0
        documents_failed = 0

        try:
            workspaces_resp = await with_retry(
                lambda: self.http_client.list_workspaces(access_token),
                max_retries=2,
            )
            workspaces = [
                ws.get("slug")
                for ws in workspaces_resp.get("values", []) or []
                if ws.get("slug")
            ]

            for ws in workspaces:
                page = 1
                while True:
                    repos_resp = await with_retry(
                        lambda p=page, w=ws: self.http_client.list_repositories(
                            access_token, w, pagelen=50, page=p
                        ),
                        max_retries=2,
                    )
                    repos = repos_resp.get("values", []) or []
                    for repo in repos:
                        repo_slug = repo.get("slug") or (
                            repo.get("full_name", "").split("/")[-1]
                        )
                        if not repo_slug:
                            continue
                        try:
                            prs_resp = await with_retry(
                                lambda w=ws, s=repo_slug: self.http_client.list_pull_requests(
                                    access_token, w, s, state="OPEN", pagelen=50
                                ),
                                max_retries=2,
                            )
                            for pr in prs_resp.get("values", []) or []:
                                documents_found += 1
                                try:
                                    doc: NormalizedDocument = normalize_pull_request(
                                        pr,
                                        self.connector_id,
                                        self.tenant_id,
                                        workspace=ws,
                                        repo_slug=repo_slug,
                                    )
                                    await self.ingest_document(
                                        doc, kb_id=kb_id or "", webhook_url=webhook_url
                                    )
                                    documents_synced += 1
                                except Exception as exc:  # noqa: BLE001
                                    logger.error(
                                        "bitbucket.sync.pr_failed",
                                        pr_id=pr.get("id"),
                                        error=str(exc),
                                    )
                                    documents_failed += 1
                        except BitbucketError as exc:
                            logger.error(
                                "bitbucket.sync.repo_failed",
                                workspace=ws,
                                repo=repo_slug,
                                error=str(exc),
                            )

                    if not repos_resp.get("next"):
                        break
                    page += 1

            status = (
                SyncStatus.COMPLETED if documents_failed == 0 else SyncStatus.PARTIAL
            )
            return SyncResult(
                status=status,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=f"Synced {documents_synced}/{documents_found} pull requests",
            )

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "bitbucket.sync.failed", error=str(exc), connector_id=self.connector_id
            )
            return SyncResult(
                status=SyncStatus.FAILED,
                documents_found=documents_found,
                documents_synced=documents_synced,
                documents_failed=documents_failed,
                message=str(exc),
            )

    # ── Public API methods (per implementation_plan.md Section 5) ──────────

    async def get_current_user(self) -> Dict[str, Any]:
        """GET /user — return current authenticated user (raw dict)."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get_user(access_token),
            max_retries=2,
        )

    async def list_workspaces(
        self, role: str = "member", pagelen: int = 50
    ) -> Dict[str, Any]:
        """GET /workspaces — list workspaces the authenticated user belongs to."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_workspaces(
                access_token, role=role, pagelen=pagelen
            ),
            max_retries=3,
        )

    async def get_workspace(self, workspace: str) -> Dict[str, Any]:
        """GET /workspaces/{workspace}."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get_workspace(access_token, workspace),
            max_retries=3,
        )

    async def list_repositories(
        self,
        workspace: str,
        role: str = "member",
        pagelen: int = 50,
        page: int = 1,
    ) -> Dict[str, Any]:
        """GET /repositories/{workspace} — list repositories in a workspace."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_repositories(
                access_token, workspace, role=role, pagelen=pagelen, page=page
            ),
            max_retries=3,
        )

    async def get_repository(self, workspace: str, repo_slug: str) -> Dict[str, Any]:
        """GET /repositories/{workspace}/{repo_slug}."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get_repository(access_token, workspace, repo_slug),
            max_retries=3,
        )

    async def create_repository(
        self,
        workspace: str,
        repo_slug: str,
        scm: str = "git",
        is_private: bool = True,
        description: str = "",
        project_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /repositories/{workspace}/{repo_slug}."""
        body: Dict[str, Any] = {
            "scm": scm,
            "is_private": is_private,
        }
        if description:
            body["description"] = description
        if project_key:
            body["project"] = {"key": project_key}
        access_token = await self._get_valid_token()
        return await self.http_client.create_repository(
            access_token, workspace, repo_slug, body
        )

    async def delete_repository(
        self, workspace: str, repo_slug: str
    ) -> Dict[str, Any]:
        """DELETE /repositories/{workspace}/{repo_slug}."""
        access_token = await self._get_valid_token()
        return await self.http_client.delete_repository(
            access_token, workspace, repo_slug
        )

    async def list_branches(
        self, workspace: str, repo_slug: str, pagelen: int = 50
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/refs/branches."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_branches(
                access_token, workspace, repo_slug, pagelen=pagelen
            ),
            max_retries=3,
        )

    async def list_pull_requests(
        self,
        workspace: str,
        repo_slug: str,
        state: str = "OPEN",
        pagelen: int = 50,
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/pullrequests."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_pull_requests(
                access_token, workspace, repo_slug, state=state, pagelen=pagelen
            ),
            max_retries=3,
        )

    async def get_pull_request(
        self, workspace: str, repo_slug: str, pull_id: int
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/pullrequests/{id}."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get_pull_request(
                access_token, workspace, repo_slug, pull_id
            ),
            max_retries=3,
        )

    async def create_pull_request(
        self,
        workspace: str,
        repo_slug: str,
        title: str,
        source_branch: str,
        destination_branch: str = "main",
        description: str = "",
        reviewers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """POST /repositories/{ws}/{slug}/pullrequests.

        ``reviewers`` is a list of Bitbucket account UUIDs (with surrounding
        braces) or account_ids.
        """
        body: Dict[str, Any] = {
            "title": title,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": destination_branch}},
            "description": description or "",
        }
        if reviewers:
            body["reviewers"] = [{"uuid": uid} for uid in reviewers]

        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.create_pull_request(
                access_token, workspace, repo_slug, body
            ),
            max_retries=2,
        )

    async def merge_pull_request(
        self,
        workspace: str,
        repo_slug: str,
        pull_id: int,
        merge_strategy: str = "merge_commit",
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /repositories/{ws}/{slug}/pullrequests/{id}/merge."""
        body: Dict[str, Any] = {"merge_strategy": merge_strategy}
        if message:
            body["message"] = message

        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.merge_pull_request(
                access_token, workspace, repo_slug, pull_id, body
            ),
            max_retries=2,
        )

    async def list_issues(
        self,
        workspace: str,
        repo_slug: str,
        state: str = "new",
        pagelen: int = 50,
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/issues — state filter via ?q=."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_issues(
                access_token, workspace, repo_slug, state=state, pagelen=pagelen
            ),
            max_retries=3,
        )

    async def get_issue(
        self, workspace: str, repo_slug: str, issue_id: int
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/issues/{id}."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get_issue(
                access_token, workspace, repo_slug, issue_id
            ),
            max_retries=3,
        )

    async def create_issue(
        self,
        workspace: str,
        repo_slug: str,
        title: str,
        content: str = "",
        priority: str = "minor",
        kind: str = "bug",
    ) -> Dict[str, Any]:
        """POST /repositories/{ws}/{slug}/issues."""
        body: Dict[str, Any] = {
            "title": title,
            "priority": priority,
            "kind": kind,
        }
        if content:
            body["content"] = {"raw": content}

        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.create_issue(
                access_token, workspace, repo_slug, body
            ),
            max_retries=2,
        )

    async def list_commits(
        self,
        workspace: str,
        repo_slug: str,
        branch: Optional[str] = None,
        pagelen: int = 50,
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/commits[/{branch}]."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_commits(
                access_token, workspace, repo_slug, branch=branch, pagelen=pagelen
            ),
            max_retries=3,
        )

    async def get_commit(
        self, workspace: str, repo_slug: str, commit: str
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/commit/{node}."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get_commit(
                access_token, workspace, repo_slug, commit
            ),
            max_retries=3,
        )

    async def list_webhooks(
        self, workspace: str, repo_slug: str
    ) -> Dict[str, Any]:
        """GET /repositories/{ws}/{slug}/hooks."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.list_webhooks(access_token, workspace, repo_slug),
            max_retries=3,
        )

    async def create_webhook(
        self,
        workspace: str,
        repo_slug: str,
        description: str,
        url: str,
        events: Optional[List[str]] = None,
        active: bool = True,
    ) -> Dict[str, Any]:
        """POST /repositories/{ws}/{slug}/hooks."""
        body: Dict[str, Any] = {
            "description": description,
            "url": url,
            "active": active,
            "events": events or ["repo:push"],
        }
        access_token = await self._get_valid_token()
        return await self.http_client.create_webhook(
            access_token, workspace, repo_slug, body
        )

    async def get_file_content(
        self, workspace: str, repo_slug: str, commit: str, path: str
    ) -> str:
        """GET /repositories/{ws}/{slug}/src/{commit}/{path} — raw text."""
        access_token = await self._get_valid_token()
        return await with_retry(
            lambda: self.http_client.get_file_content(
                access_token, workspace, repo_slug, commit, path
            ),
            max_retries=3,
        )
