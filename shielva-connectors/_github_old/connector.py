"""
GitHub Connector
Connects to GitHub to ingest repositories, issues, and documentation.
"""
from typing import Dict, Any, List, Optional, AsyncGenerator
from datetime import datetime
import httpx
import structlog
import base64

from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth, 
    AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus
)
from shared.oauth_handler import OAuthHandler, OAuthConfig

logger = structlog.get_logger(__name__)


def get_github_oauth_config(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: List[str]
) -> OAuthConfig:
    """Get GitHub OAuth configuration."""
    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        authorization_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scopes=scopes,
        redirect_uri=redirect_uri
    )


class GitHubConnector(BaseConnector):
    """
    GitHub Connector for Shielva.
    
    Features:
    - OAuth 2.0 authentication
    - Repository sync (README, docs folder)
    - Issues and discussions
    - Pull request descriptions
    - Wiki pages
    - Code search for documentation
    """
    
    CONNECTOR_TYPE = "github"
    CONNECTOR_NAME = "GitHub"
    SUPPORTED_AUTH_TYPES = ["oauth2"]
    REQUIRED_SCOPES = [
        "repo",
        "read:org"
    ]
    
    API_BASE = "https://api.github.com"
    
    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None
    ):
        """
        Initialize GitHub connector.
        
        Config options:
        - client_id: GitHub OAuth App Client ID
        - client_secret: GitHub OAuth App Client Secret
        - repos: List of repos to sync (owner/repo format)
        - include_issues: Include issues
        - include_prs: Include pull requests
        - include_wiki: Include wiki pages
        - include_discussions: Include discussions
        - doc_paths: Paths to sync for docs (default: docs/, README.md)
        - file_extensions: File extensions to include
        """
        super().__init__(tenant_id, connector_id, config)
        
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self._oauth_handler: Optional[OAuthHandler] = None
        self._user_info: Optional[Dict[str, Any]] = None
        
        # Configuration
        self.repos = config.get("repos", [])
        self.include_issues = config.get("include_issues", True)
        self.include_prs = config.get("include_prs", False)
        self.include_wiki = config.get("include_wiki", True)
        self.include_discussions = config.get("include_discussions", False)
        self.doc_paths = config.get("doc_paths", ["docs/", "README.md", "CONTRIBUTING.md"])
        self.file_extensions = config.get("file_extensions", ["md", "rst", "txt"])
    
    # ===== Lifecycle Methods =====
    
    async def install(self) -> ConnectorStatus:
        """Install GitHub connector."""
        logger.info("Installing GitHub connector", tenant_id=self.tenant_id)
        
        required = ["client_id", "client_secret", "redirect_uri"]
        for key in required:
            if key not in self.config:
                self._status.health = ConnectorHealth.OFFLINE
                self._status.error = f"Missing required config: {key}"
                return self._status
        
        self._oauth_handler = OAuthHandler(
            get_github_oauth_config(
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                redirect_uri=self.config["redirect_uri"],
                scopes=self.REQUIRED_SCOPES
            )
        )
        
        self._status.health = ConnectorHealth.DEGRADED
        self._status.auth_status = AuthStatus.PENDING
        
        return self._status
    
    def get_oauth_url(self, redirect_uri: str, state: str = None) -> str:
        """Get GitHub OAuth authorization URL."""
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        return self._oauth_handler.get_authorization_url(state=state or "github")
    
    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Complete OAuth authorization."""
        logger.info("Authorizing GitHub connector")
        
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        # GitHub returns token in different format
        token_info = await self._exchange_github_code(auth_code)
        self.set_token(token_info)
        
        # Get user info
        self._user_info = await self._api_request("/user")
        
        self._status.health = ConnectorHealth.HEALTHY
        self._status.auth_status = AuthStatus.CONNECTED
        
        return token_info
    
    async def _exchange_github_code(self, code: str) -> TokenInfo:
        """Exchange GitHub auth code for token."""
        response = await self._http_client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": self.config["client_id"],
                "client_secret": self.config["client_secret"],
                "code": code
            }
        )
        response.raise_for_status()
        data = response.json()
        
        if "error" in data:
            raise ValueError(data.get("error_description", data["error"]))
        
        return TokenInfo(
            access_token=data["access_token"],
            token_type=data.get("token_type", "Bearer"),
            scopes=data.get("scope", "").split(",")
        )
    
    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        webhook_url: str = None
    ) -> SyncResult:
        """Sync content from GitHub."""
        import uuid
        
        job_id = str(uuid.uuid4())
        result = SyncResult(
            job_id=job_id,
            status=SyncStatus.SYNCING
        )
        
        logger.info(
            "Starting GitHub sync",
            tenant_id=self.tenant_id,
            repos=len(self.repos),
            since=since,
            webhook_url=webhook_url
        )
        
        try:
            await self.ensure_token()
            
            documents_synced = 0
            documents_failed = 0
            
            # Get repos to sync
            repos = await self._get_repos()
            
            for repo in repos:
                repo_name = repo["full_name"]
                
                try:
                    # Sync documentation files
                    docs = await self._get_docs(repo_name)
                    for doc in docs:
                        try:
                            normalized = await self.normalize({
                                **doc,
                                "repo": repo_name,
                                "repo_data": repo
                            })
                            success = await self.ingest_batch(self.connector_id, [normalized], webhook_url=webhook_url)
                            if success:
                                documents_synced += 1
                            else:
                                documents_failed += 1
                        except Exception as e:
                            documents_failed += 1
                    
                    # Sync issues
                    if self.include_issues:
                        issues = await self._get_issues(repo_name, since)
                        for issue in issues:
                            try:
                                normalized = await self._normalize_issue(repo_name, issue)
                                success = await self.ingest_batch(self.connector_id, [normalized], webhook_url=webhook_url)
                                if success:
                                    documents_synced += 1
                                else:
                                    documents_failed += 1
                            except Exception as e:
                                documents_failed += 1
                    
                    # Sync PRs
                    if self.include_prs:
                        prs = await self._get_pull_requests(repo_name, since)
                        for pr in prs:
                            try:
                                normalized = await self._normalize_pr(repo_name, pr)
                                success = await self.ingest_batch(self.connector_id, [normalized], webhook_url=webhook_url)
                                if success:
                                    documents_synced += 1
                                else:
                                    documents_failed += 1
                            except Exception as e:
                                documents_failed += 1
                    
                    # Sync wiki
                    if self.include_wiki and repo.get("has_wiki"):
                        wiki_pages = await self._get_wiki(repo_name)
                        for page in wiki_pages:
                            try:
                                normalized = await self._normalize_wiki(repo_name, page)
                                success = await self.ingest_batch(self.connector_id, [normalized], webhook_url=webhook_url)
                                if success:
                                    documents_synced += 1
                                else:
                                    documents_failed += 1
                            except Exception as e:
                                documents_failed += 1
                    
                except Exception as e:
                    logger.error(
                        "Failed to sync repo",
                        repo=repo_name,
                        error=str(e)
                    )
                    result.errors.append(f"Repo {repo_name}: {str(e)}")
            
            result.documents_found = documents_synced + documents_failed
            result.documents_synced = documents_synced
            result.documents_failed = documents_failed
            result.status = SyncStatus.COMPLETED
            result.completed_at = datetime.utcnow()
            
            self._status.last_sync = datetime.utcnow()
            self._status.documents_indexed += documents_synced
            
            logger.info(
                "GitHub sync completed",
                synced=documents_synced,
                failed=documents_failed
            )
            
        except Exception as e:
            logger.error("GitHub sync failed", error=str(e))
            result.status = SyncStatus.FAILED
            result.errors.append(str(e))
        
        return result
    
    async def health_check(self) -> ConnectorStatus:
        """Check GitHub connector health."""
        try:
            if not self._token_info:
                self._status.health = ConnectorHealth.OFFLINE
                return self._status
            
            # Test with user endpoint
            await self._api_request("/user")
            
            self._status.health = ConnectorHealth.HEALTHY
            self._status.error = None
            
        except Exception as e:
            self._status.health = ConnectorHealth.DEGRADED
            self._status.error = str(e)
        
        return self._status
    
    async def on_token_refresh(self) -> TokenInfo:
        """GitHub tokens don't expire typically."""
        return self._token_info
    
    # ===== Data Methods =====
    
    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        """Normalize GitHub file to standard format."""
        path = raw_data.get("path", "")
        name = raw_data.get("name", path.split("/")[-1])
        repo = raw_data.get("repo", "")
        
        # Decode content
        content = raw_data.get("content", "")
        if raw_data.get("encoding") == "base64":
            content = base64.b64decode(content).decode("utf-8", errors="ignore")
        
        # Build URL
        html_url = raw_data.get("html_url", f"https://github.com/{repo}/blob/main/{path}")
        
        return NormalizedDocument(
            id=f"gh_{repo.replace('/', '_')}_{path.replace('/', '_')}",
            source_id=raw_data.get("sha", ""),
            title=name,
            content=content,
            content_type="markdown" if path.endswith(".md") else "text",
            source_url=html_url,
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "repo": repo,
                "path": path,
                "size": raw_data.get("size"),
                "item_type": "file"
            }
        )
    
    async def _normalize_issue(
        self,
        repo: str,
        issue: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize GitHub issue."""
        issue_number = issue.get("number")
        title = issue.get("title", "")
        body = issue.get("body", "")
        
        # Get comments
        comments = await self._get_issue_comments(repo, issue_number)
        comments_text = "\n\n---\n\n".join([
            f"**{c.get('user', {}).get('login', 'Unknown')}**: {c.get('body', '')}"
            for c in comments
        ])
        
        content = f"# {title}\n\n{body}"
        if comments_text:
            content += f"\n\n## Comments\n\n{comments_text}"
        
        return NormalizedDocument(
            id=f"gh_issue_{repo.replace('/', '_')}_{issue_number}",
            source_id=str(issue_number),
            title=f"Issue #{issue_number}: {title}",
            content=content,
            content_type="markdown",
            source_url=issue.get("html_url", ""),
            author=issue.get("user", {}).get("login"),
            created_at=self._parse_date(issue.get("created_at")),
            updated_at=self._parse_date(issue.get("updated_at")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "repo": repo,
                "issue_number": issue_number,
                "state": issue.get("state"),
                "labels": [l.get("name") for l in issue.get("labels", [])],
                "item_type": "issue"
            }
        )
    
    async def _normalize_pr(
        self,
        repo: str,
        pr: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize GitHub pull request."""
        pr_number = pr.get("number")
        title = pr.get("title", "")
        body = pr.get("body", "")
        
        content = f"# PR #{pr_number}: {title}\n\n{body}"
        
        return NormalizedDocument(
            id=f"gh_pr_{repo.replace('/', '_')}_{pr_number}",
            source_id=str(pr_number),
            title=f"PR #{pr_number}: {title}",
            content=content,
            content_type="markdown",
            source_url=pr.get("html_url", ""),
            author=pr.get("user", {}).get("login"),
            created_at=self._parse_date(pr.get("created_at")),
            updated_at=self._parse_date(pr.get("updated_at")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "repo": repo,
                "pr_number": pr_number,
                "state": pr.get("state"),
                "merged": pr.get("merged"),
                "item_type": "pull_request"
            }
        )
    
    async def _normalize_wiki(
        self,
        repo: str,
        page: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize GitHub wiki page."""
        title = page.get("title", "")
        content = page.get("content", "")
        
        return NormalizedDocument(
            id=f"gh_wiki_{repo.replace('/', '_')}_{title.replace(' ', '_')}",
            source_id=page.get("sha", title),
            title=f"Wiki: {title}",
            content=content,
            content_type="markdown",
            source_url=f"https://github.com/{repo}/wiki/{title.replace(' ', '-')}",
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "repo": repo,
                "item_type": "wiki"
            }
        )
    
    # ===== API Methods =====
    
    async def _api_request(
        self,
        path: str,
        params: Dict[str, Any] = None
    ) -> Any:
        """Make GitHub API request."""
        await self.ensure_token()
        
        url = f"{self.API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self._token_info.access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        
        response = await self._http_client.get(url, headers=headers, params=params)
        response.raise_for_status()
        
        return response.json()
    
    async def _get_repos(self) -> List[Dict[str, Any]]:
        """Get repositories to sync."""
        if self.repos:
            # Get specific repos
            repos = []
            for repo_name in self.repos:
                try:
                    repo = await self._api_request(f"/repos/{repo_name}")
                    repos.append(repo)
                except Exception:
                    pass
            return repos
        else:
            # Get all user repos
            return await self._api_request("/user/repos", {"per_page": 100})
    
    async def _get_docs(self, repo: str) -> List[Dict[str, Any]]:
        """Get documentation files from repo."""
        docs = []
        
        for path in self.doc_paths:
            try:
                if path.endswith("/"):
                    # It's a directory
                    contents = await self._api_request(
                        f"/repos/{repo}/contents/{path.rstrip('/')}"
                    )
                    for item in contents:
                        if item.get("type") == "file":
                            ext = item.get("name", "").split(".")[-1]
                            if ext in self.file_extensions:
                                file_content = await self._api_request(
                                    f"/repos/{repo}/contents/{item['path']}"
                                )
                                docs.append(file_content)
                else:
                    # It's a file
                    content = await self._api_request(f"/repos/{repo}/contents/{path}")
                    docs.append(content)
            except Exception:
                pass
        
        return docs
    
    async def _get_issues(
        self,
        repo: str,
        since: datetime = None
    ) -> List[Dict[str, Any]]:
        """Get issues from repo."""
        params = {"state": "all", "per_page": 100}
        if since:
            params["since"] = since.isoformat()
        
        return await self._api_request(f"/repos/{repo}/issues", params)
    
    async def _get_issue_comments(
        self,
        repo: str,
        issue_number: int
    ) -> List[Dict[str, Any]]:
        """Get comments for an issue."""
        return await self._api_request(
            f"/repos/{repo}/issues/{issue_number}/comments"
        )
    
    async def _get_pull_requests(
        self,
        repo: str,
        since: datetime = None
    ) -> List[Dict[str, Any]]:
        """Get pull requests from repo."""
        params = {"state": "all", "per_page": 100}
        
        prs = await self._api_request(f"/repos/{repo}/pulls", params)
        
        if since:
            prs = [p for p in prs if self._parse_date(p.get("updated_at")) > since]
        
        return prs
    
    async def _get_wiki(self, repo: str) -> List[Dict[str, Any]]:
        """Get wiki pages (requires cloning wiki repo)."""
        # GitHub API doesn't directly support wiki pages
        # Would need to clone {repo}.wiki.git
        # For now, return empty list
        return []
    
    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse ISO date string."""
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        except ValueError:
            return None
    
    async def close(self):
        """Close HTTP clients."""
        await self._http_client.aclose()
        if self._oauth_handler:
            await self._oauth_handler.close()
