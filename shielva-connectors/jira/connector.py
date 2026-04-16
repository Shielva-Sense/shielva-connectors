"""
Jira Connector
Connects to Atlassian Jira to ingest issues, comments, and project data.
"""
from typing import Dict, Any, List, Optional, AsyncGenerator
from datetime import datetime
import httpx
import structlog

from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth, 
    AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus
)
from shared.oauth_handler import OAuthHandler, get_atlassian_oauth_config

logger = structlog.get_logger(__name__)


class JiraConnector(BaseConnector):
    """
    Jira Connector for Shielva.
    
    Features:
    - OAuth 2.0 authentication (Atlassian Cloud)
    - Issue retrieval with full description
    - Comment extraction
    - Attachment metadata
    - JQL query support
    - Incremental sync via updated date
    """
    
    CONNECTOR_TYPE = "jira"
    CONNECTOR_NAME = "Atlassian Jira"
    SUPPORTED_AUTH_TYPES = ["oauth2"]
    REQUIRED_SCOPES = [
        "read:jira-work",
        "read:jira-user",
        "offline_access"
    ]
    
    API_BASE = "https://api.atlassian.com/ex/jira"
    
    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None
    ):
        """
        Initialize Jira connector.
        
        Config options:
        - client_id: OAuth client ID
        - client_secret: OAuth client secret
        - project_keys: List of project keys to sync (optional, syncs all if empty)
        - issue_types: List of issue types to include (optional)
        - include_comments: Include issue comments
        - include_attachments: Include attachment metadata
        - jql_filter: Additional JQL filter
        - max_issues_per_sync: Maximum issues per sync
        """
        super().__init__(tenant_id, connector_id, config)
        
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self._oauth_handler: Optional[OAuthHandler] = None
        self._cloud_id: Optional[str] = None
        
        # Configuration
        self.project_keys = config.get("project_keys", [])
        self.issue_types = config.get("issue_types", [])
        self.include_comments = config.get("include_comments", True)
        self.include_attachments = config.get("include_attachments", False)
        self.jql_filter = config.get("jql_filter", "")
        self.max_issues_per_sync = config.get("max_issues_per_sync", 1000)
    
    # ===== Lifecycle Methods =====
    
    async def install(self) -> ConnectorStatus:
        """Install Jira connector."""
        logger.info("Installing Jira connector", tenant_id=self.tenant_id)
        
        required = ["client_id", "client_secret", "redirect_uri"]
        for key in required:
            if key not in self.config:
                self._status.health = ConnectorHealth.OFFLINE
                self._status.error = f"Missing required config: {key}"
                return self._status
        
        self._oauth_handler = OAuthHandler(
            get_atlassian_oauth_config(
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
        """Get Jira OAuth authorization URL."""
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        return self._oauth_handler.get_authorization_url(state=state or "jira")
    
    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Complete OAuth authorization."""
        logger.info("Authorizing Jira connector")
        
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        token_info = await self._oauth_handler.exchange_code(auth_code)
        self.set_token(token_info)
        
        # Get Cloud ID
        await self._fetch_cloud_id()
        
        self._status.health = ConnectorHealth.HEALTHY
        self._status.auth_status = AuthStatus.CONNECTED
        
        logger.info("Jira connector authorized", cloud_id=self._cloud_id)
        
        return token_info
    
    async def _fetch_cloud_id(self) -> str:
        """Fetch Atlassian Cloud ID."""
        response = await self._api_request(
            "GET",
            "https://api.atlassian.com/oauth/token/accessible-resources"
        )
        
        if response and len(response) > 0:
            self._cloud_id = response[0]["id"]
            return self._cloud_id
        
        raise ValueError("No accessible Jira instances found")
    
    async def sync(
        self,
        since: datetime = None,
        full: bool = False
    ) -> SyncResult:
        """Sync issues from Jira."""
        import uuid
        
        job_id = str(uuid.uuid4())
        result = SyncResult(
            job_id=job_id,
            status=SyncStatus.SYNCING
        )
        
        logger.info(
            "Starting Jira sync",
            tenant_id=self.tenant_id,
            since=since,
            full=full
        )
        
        try:
            await self.ensure_token()
            
            # Build JQL query
            jql_parts = []
            
            if self.project_keys:
                projects = ", ".join(self.project_keys)
                jql_parts.append(f"project IN ({projects})")
            
            if self.issue_types:
                types = ", ".join(f'"{t}"' for t in self.issue_types)
                jql_parts.append(f"issuetype IN ({types})")
            
            if since and not full:
                date_str = since.strftime("%Y-%m-%d %H:%M")
                jql_parts.append(f'updated >= "{date_str}"')
            
            if self.jql_filter:
                jql_parts.append(f"({self.jql_filter})")
            
            jql = " AND ".join(jql_parts) if jql_parts else "ORDER BY updated DESC"
            
            # Fetch issues
            issues = await self._search_issues(jql)
            
            documents_synced = 0
            documents_failed = 0
            
            for issue in issues:
                try:
                    # Normalize issue
                    doc = await self.normalize(issue)
                    
                    # TODO: Send to Knowledge Manager
                    documents_synced += 1
                    
                    # Get comments if enabled
                    if self.include_comments:
                        comments = await self._get_comments(issue["key"])
                        for comment in comments:
                            try:
                                comment_doc = await self._normalize_comment(
                                    issue=issue,
                                    comment=comment
                                )
                                documents_synced += 1
                            except Exception as e:
                                documents_failed += 1
                                result.errors.append(str(e))
                    
                    if documents_synced >= self.max_issues_per_sync:
                        break
                        
                except Exception as e:
                    logger.error(
                        "Failed to process issue",
                        issue_key=issue.get("key"),
                        error=str(e)
                    )
                    documents_failed += 1
                    result.errors.append(str(e))
            
            result.documents_found = documents_synced + documents_failed
            result.documents_synced = documents_synced
            result.documents_failed = documents_failed
            result.status = SyncStatus.COMPLETED
            result.completed_at = datetime.utcnow()
            
            self._status.last_sync = datetime.utcnow()
            self._status.documents_indexed += documents_synced
            
            logger.info(
                "Jira sync completed",
                synced=documents_synced,
                failed=documents_failed
            )
            
        except Exception as e:
            logger.error("Jira sync failed", error=str(e))
            result.status = SyncStatus.FAILED
            result.errors.append(str(e))
        
        return result
    
    async def health_check(self) -> ConnectorStatus:
        """Check Jira connector health."""
        try:
            if not self._token_info:
                self._status.health = ConnectorHealth.OFFLINE
                return self._status
            
            if not self.is_token_valid():
                await self.on_token_refresh()
            
            # Test with myself endpoint
            url = f"{self._get_api_base()}/rest/api/3/myself"
            await self._api_request("GET", url)
            
            self._status.health = ConnectorHealth.HEALTHY
            self._status.error = None
            
        except Exception as e:
            self._status.health = ConnectorHealth.DEGRADED
            self._status.error = str(e)
        
        return self._status
    
    async def on_token_refresh(self) -> TokenInfo:
        """Refresh Jira access token."""
        if not self._oauth_handler or not self._token_info:
            raise ValueError("No token to refresh")
        
        token_info = await self._oauth_handler.refresh_token(
            self._token_info.refresh_token
        )
        self.set_token(token_info)
        
        return token_info
    
    # ===== Data Methods =====
    
    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        """Normalize Jira issue to standard format."""
        key = raw_data.get("key", "")
        fields = raw_data.get("fields", {})
        
        # Get issue details
        summary = fields.get("summary", "")
        description = fields.get("description", {})
        
        # Convert Atlassian Document Format to text
        description_text = self._adf_to_text(description) if description else ""
        
        # Build full content
        content_parts = [
            f"# {summary}",
            "",
            f"**Issue Key:** {key}",
            f"**Status:** {fields.get('status', {}).get('name', '')}",
            f"**Priority:** {fields.get('priority', {}).get('name', '')}",
            f"**Type:** {fields.get('issuetype', {}).get('name', '')}",
            f"**Assignee:** {fields.get('assignee', {}).get('displayName', 'Unassigned')}",
            f"**Reporter:** {fields.get('reporter', {}).get('displayName', 'Unknown')}",
            "",
            "## Description",
            description_text
        ]
        
        # Add labels
        labels = fields.get("labels", [])
        if labels:
            content_parts.extend(["", f"**Labels:** {', '.join(labels)}"])
        
        content = "\n".join(content_parts)
        
        # Parse dates
        created = self._parse_date(fields.get("created"))
        updated = self._parse_date(fields.get("updated"))
        
        return NormalizedDocument(
            id=f"jira_{key}",
            source_id=raw_data.get("id", key),
            title=f"[{key}] {summary}",
            content=content,
            content_type="markdown",
            source_url=f"{self._get_browse_url()}/browse/{key}",
            author=fields.get("reporter", {}).get("displayName"),
            created_at=created,
            updated_at=updated,
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "issue_key": key,
                "project_key": fields.get("project", {}).get("key"),
                "project_name": fields.get("project", {}).get("name"),
                "issue_type": fields.get("issuetype", {}).get("name"),
                "status": fields.get("status", {}).get("name"),
                "priority": fields.get("priority", {}).get("name"),
                "labels": labels,
                "components": [c.get("name") for c in fields.get("components", [])],
                "resolution": fields.get("resolution", {}).get("name") if fields.get("resolution") else None
            }
        )
    
    async def _normalize_comment(
        self,
        issue: Dict[str, Any],
        comment: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize Jira comment."""
        key = issue.get("key", "")
        comment_id = comment.get("id", "")
        
        body = comment.get("body", {})
        body_text = self._adf_to_text(body) if body else ""
        
        author = comment.get("author", {}).get("displayName", "Unknown")
        
        content_parts = [
            f"Comment on [{key}] {issue.get('fields', {}).get('summary', '')}",
            "",
            f"**Author:** {author}",
            "",
            body_text
        ]
        
        return NormalizedDocument(
            id=f"jira_{key}_comment_{comment_id}",
            source_id=comment_id,
            title=f"Comment on {key}",
            content="\n".join(content_parts),
            content_type="markdown",
            source_url=f"{self._get_browse_url()}/browse/{key}?focusedCommentId={comment_id}",
            author=author,
            created_at=self._parse_date(comment.get("created")),
            updated_at=self._parse_date(comment.get("updated")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "issue_key": key,
                "comment_id": comment_id,
                "is_comment": True
            },
            parent_id=f"jira_{key}"
        )
    
    # ===== API Methods =====
    
    async def _api_request(
        self,
        method: str,
        url: str,
        params: Dict[str, Any] = None,
        json: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Make authenticated API request."""
        await self.ensure_token()
        
        headers = {
            "Authorization": f"Bearer {self._token_info.access_token}",
            "Accept": "application/json"
        }
        
        response = await self._http_client.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json
        )
        response.raise_for_status()
        
        return response.json()
    
    def _get_api_base(self) -> str:
        """Get API base URL with cloud ID."""
        return f"{self.API_BASE}/{self._cloud_id}"
    
    def _get_browse_url(self) -> str:
        """Get Jira browse URL."""
        # This would come from the accessible-resources response
        return f"https://yoursite.atlassian.net"
    
    async def _search_issues(self, jql: str) -> List[Dict[str, Any]]:
        """Search issues using JQL."""
        url = f"{self._get_api_base()}/rest/api/3/search"
        
        all_issues = []
        start_at = 0
        max_results = 100
        
        while len(all_issues) < self.max_issues_per_sync:
            params = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": max_results,
                "fields": "summary,description,status,priority,issuetype,assignee,reporter,labels,components,created,updated,resolution,project"
            }
            
            response = await self._api_request("GET", url, params=params)
            
            issues = response.get("issues", [])
            all_issues.extend(issues)
            
            if len(issues) < max_results:
                break
            
            start_at += max_results
        
        return all_issues[:self.max_issues_per_sync]
    
    async def _get_comments(self, issue_key: str) -> List[Dict[str, Any]]:
        """Get comments for an issue."""
        url = f"{self._get_api_base()}/rest/api/3/issue/{issue_key}/comment"
        
        response = await self._api_request("GET", url)
        return response.get("comments", [])
    
    # ===== Utility Methods =====
    
    def _adf_to_text(self, adf: Dict[str, Any]) -> str:
        """Convert Atlassian Document Format to plain text."""
        if not adf:
            return ""
        
        def extract_text(node: Dict[str, Any]) -> str:
            if not isinstance(node, dict):
                return ""
            
            text_parts = []
            
            if node.get("type") == "text":
                text_parts.append(node.get("text", ""))
            
            for child in node.get("content", []):
                text_parts.append(extract_text(child))
            
            return "".join(text_parts)
        
        return extract_text(adf)
    
    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse Jira date string."""
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
