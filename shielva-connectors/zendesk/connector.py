"""
Zendesk Connector
Connects to Zendesk to ingest tickets, help center articles, and macros.
"""
from typing import Dict, Any, List, Optional, AsyncGenerator
from datetime import datetime
import httpx
import structlog

from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth, 
    AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus
)
from shared.oauth_handler import OAuthHandler, OAuthConfig

logger = structlog.get_logger(__name__)


def get_zendesk_oauth_config(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    subdomain: str,
    scopes: List[str]
) -> OAuthConfig:
    """Get Zendesk OAuth configuration."""
    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        authorization_url=f"https://{subdomain}.zendesk.com/oauth/authorizations/new",
        token_url=f"https://{subdomain}.zendesk.com/oauth/tokens",
        scopes=scopes,
        redirect_uri=redirect_uri
    )


class ZendeskConnector(BaseConnector):
    """
    Zendesk Connector for Shielva.
    
    Features:
    - OAuth 2.0 authentication
    - Ticket sync with comments
    - Help Center articles
    - Community posts
    - Macros and automations
    - Categories and sections
    """
    
    CONNECTOR_TYPE = "zendesk"
    CONNECTOR_NAME = "Zendesk"
    SUPPORTED_AUTH_TYPES = ["oauth2"]
    REQUIRED_SCOPES = ["read", "tickets:read", "hc:read"]
    
    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None
    ):
        """
        Initialize Zendesk connector.
        
        Config options:
        - client_id: Zendesk OAuth Client ID
        - client_secret: Zendesk OAuth Client Secret
        - subdomain: Zendesk subdomain
        - include_tickets: Include support tickets
        - include_articles: Include help center articles
        - include_community: Include community posts
        - include_macros: Include macro definitions
        - ticket_status: Filter tickets by status
        - categories: Help center categories to sync
        - days_to_sync: Days of tickets to sync
        """
        super().__init__(tenant_id, connector_id, config)
        
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self._oauth_handler: Optional[OAuthHandler] = None
        
        # Configuration
        self.subdomain = config.get("subdomain", "")
        self.include_tickets = config.get("include_tickets", True)
        self.include_articles = config.get("include_articles", True)
        self.include_community = config.get("include_community", False)
        self.include_macros = config.get("include_macros", False)
        self.ticket_status = config.get("ticket_status", ["solved", "closed"])
        self.categories = config.get("categories", [])
        self.days_to_sync = config.get("days_to_sync", 90)
        
        self._api_base = f"https://{self.subdomain}.zendesk.com/api/v2"
    
    # ===== Lifecycle Methods =====
    
    async def install(self) -> ConnectorStatus:
        """Install Zendesk connector."""
        logger.info("Installing Zendesk connector", tenant_id=self.tenant_id)
        
        required = ["client_id", "client_secret", "redirect_uri", "subdomain"]
        for key in required:
            if key not in self.config:
                self._status.health = ConnectorHealth.OFFLINE
                self._status.error = f"Missing required config: {key}"
                return self._status
        
        self.subdomain = self.config["subdomain"]
        self._api_base = f"https://{self.subdomain}.zendesk.com/api/v2"
        
        self._oauth_handler = OAuthHandler(
            get_zendesk_oauth_config(
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                redirect_uri=self.config["redirect_uri"],
                subdomain=self.subdomain,
                scopes=self.REQUIRED_SCOPES
            )
        )
        
        self._status.health = ConnectorHealth.DEGRADED
        self._status.auth_status = AuthStatus.PENDING
        
        return self._status
    
    def get_oauth_url(self, redirect_uri: str, state: str = None) -> str:
        """Get Zendesk OAuth authorization URL."""
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        return self._oauth_handler.get_authorization_url(state=state or "zendesk")
    
    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Complete OAuth authorization."""
        logger.info("Authorizing Zendesk connector")
        
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        token_info = await self._oauth_handler.exchange_code(auth_code)
        self.set_token(token_info)
        
        self._status.health = ConnectorHealth.HEALTHY
        self._status.auth_status = AuthStatus.CONNECTED
        
        return token_info
    
    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        webhook_url: str = None
    ) -> SyncResult:
        """Sync content from Zendesk."""
        import uuid
        from datetime import timedelta
        
        job_id = str(uuid.uuid4())
        result = SyncResult(
            job_id=job_id,
            status=SyncStatus.SYNCING
        )
        
        logger.info(
            "Starting Zendesk sync",
            tenant_id=self.tenant_id,
            subdomain=self.subdomain,
            webhook_url=webhook_url
        )
        
        try:
            await self.ensure_token()
            
            documents_synced = 0
            documents_failed = 0
            
            # Sync tickets
            if self.include_tickets:
                # Calculate start date
                start_date = since
                if not start_date or full:
                    start_date = datetime.utcnow() - timedelta(days=self.days_to_sync)
                
                tickets = await self._get_tickets(since=start_date)
                
                for tickets_batch in [tickets[i:i+20] for i in range(0, len(tickets), 20)]:
                    batch_docs = []
                    for ticket in tickets_batch:
                        try:
                            # Get ticket comments
                            comments = await self._get_ticket_comments(ticket["id"])
                            doc = await self._normalize_ticket(ticket, comments)
                            batch_docs.append(doc)
                        except Exception as e:
                            documents_failed += 1
                            result.errors.append(str(e))
                    
                    # Ingest batch
                    if batch_docs:
                        success = await self.ingest_batch(self.connector_id, batch_docs, webhook_url=webhook_url)
                        if success:
                            documents_synced += len(batch_docs)
                        else:
                            documents_failed += len(batch_docs)

            # Sync help center articles
            if self.include_articles:
                articles = await self._get_articles(since if not full else None)
                
                batch_docs = []
                for article in articles:
                    try:
                        doc = await self.normalize(article)
                        batch_docs.append(doc)
                    except Exception as e:
                        documents_failed += 1
                        result.errors.append(str(e))
                
                # Ingest batch
                if batch_docs:
                    success = await self.ingest_batch(self.connector_id, batch_docs, webhook_url=webhook_url)
                    if success:
                        documents_synced += len(batch_docs)
                    else:
                        documents_failed += len(batch_docs)
            
            # Sync community posts
            if self.include_community:
                posts = await self._get_community_posts()
                
                batch_docs = []
                for post in posts:
                    try:
                        doc = await self._normalize_community_post(post)
                        batch_docs.append(doc)
                    except Exception as e:
                        documents_failed += 1
                
                # Ingest batch
                if batch_docs:
                    success = await self.ingest_batch(self.connector_id, batch_docs, webhook_url=webhook_url)
                    if success:
                        documents_synced += len(batch_docs)
                    else:
                        documents_failed += len(batch_docs)
            
            # Sync macros
            if self.include_macros:
                macros = await self._get_macros()
                
                batch_docs = []
                for macro in macros:
                    try:
                        doc = await self._normalize_macro(macro)
                        batch_docs.append(doc)
                    except Exception as e:
                        documents_failed += 1
                
                # Ingest batch
                if batch_docs:
                    success = await self.ingest_batch(self.connector_id, batch_docs, webhook_url=webhook_url)
                    if success:
                        documents_synced += len(batch_docs)
                    else:
                        documents_failed += len(batch_docs)
            
            result.documents_found = documents_synced + documents_failed
            result.documents_synced = documents_synced
            result.documents_failed = documents_failed
            result.status = SyncStatus.COMPLETED
            result.completed_at = datetime.utcnow()
            
            self._status.last_sync = datetime.utcnow()
            self._status.documents_indexed += documents_synced
            
            logger.info(
                "Zendesk sync completed",
                synced=documents_synced,
                failed=documents_failed
            )
            
        except Exception as e:
            logger.error("Zendesk sync failed", error=str(e))
            result.status = SyncStatus.FAILED
            result.errors.append(str(e))
        
        return result
    
    async def health_check(self) -> ConnectorStatus:
        """Check Zendesk connector health."""
        try:
            if not self._token_info:
                self._status.health = ConnectorHealth.OFFLINE
                return self._status
            
            if not self.is_token_valid():
                await self.on_token_refresh()
            
            # Test with users/me endpoint
            await self._api_request("/users/me")
            
            self._status.health = ConnectorHealth.HEALTHY
            self._status.error = None
            
        except Exception as e:
            self._status.health = ConnectorHealth.DEGRADED
            self._status.error = str(e)
        
        return self._status
    
    async def on_token_refresh(self) -> TokenInfo:
        """Refresh Zendesk access token."""
        if not self._oauth_handler or not self._token_info:
            raise ValueError("No token to refresh")
        
        token_info = await self._oauth_handler.refresh_token(
            self._token_info.refresh_token
        )
        self.set_token(token_info)
        
        return token_info
    
    # ===== Data Methods =====
    
    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        """Normalize Zendesk help center article."""
        article_id = raw_data.get("id", "")
        title = raw_data.get("title", "")
        body = raw_data.get("body", "")
        
        # Strip HTML from body
        import re
        clean_body = re.sub(r'<[^>]+>', '', body)
        
        return NormalizedDocument(
            id=f"zd_article_{article_id}",
            source_id=str(article_id),
            title=title,
            content=f"# {title}\n\n{clean_body}",
            content_type="markdown",
            source_url=raw_data.get("html_url", ""),
            author=raw_data.get("author_id"),
            created_at=self._parse_date(raw_data.get("created_at")),
            updated_at=self._parse_date(raw_data.get("updated_at")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "section_id": raw_data.get("section_id"),
                "locale": raw_data.get("locale"),
                "labels": raw_data.get("label_names", []),
                "vote_sum": raw_data.get("vote_sum"),
                "item_type": "article"
            }
        )
    
    async def _normalize_ticket(
        self,
        ticket: Dict[str, Any],
        comments: List[Dict[str, Any]]
    ) -> NormalizedDocument:
        """Normalize Zendesk ticket with comments."""
        ticket_id = ticket.get("id", "")
        subject = ticket.get("subject", "")
        description = ticket.get("description", "")
        
        # Build content with comments
        content_parts = [
            f"# Ticket #{ticket_id}: {subject}",
            "",
            f"**Status:** {ticket.get('status')}",
            f"**Priority:** {ticket.get('priority')}",
            "",
            "## Description",
            description,
            "",
            "## Comments"
        ]
        
        for comment in comments:
            author = comment.get("author_id", "Agent")
            body = comment.get("body", "")
            public = "Public" if comment.get("public") else "Internal"
            content_parts.append(f"\n**{author}** ({public}):\n{body}")
        
        return NormalizedDocument(
            id=f"zd_ticket_{ticket_id}",
            source_id=str(ticket_id),
            title=f"Ticket #{ticket_id}: {subject}",
            content="\n".join(content_parts),
            content_type="markdown",
            source_url=f"https://{self.subdomain}.zendesk.com/agent/tickets/{ticket_id}",
            created_at=self._parse_date(ticket.get("created_at")),
            updated_at=self._parse_date(ticket.get("updated_at")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "status": ticket.get("status"),
                "priority": ticket.get("priority"),
                "type": ticket.get("type"),
                "tags": ticket.get("tags", []),
                "satisfaction_rating": ticket.get("satisfaction_rating"),
                "item_type": "ticket"
            }
        )
    
    async def _normalize_community_post(
        self,
        post: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize community post."""
        post_id = post.get("id", "")
        title = post.get("title", "")
        details = post.get("details", "")
        
        return NormalizedDocument(
            id=f"zd_post_{post_id}",
            source_id=str(post_id),
            title=title,
            content=f"# {title}\n\n{details}",
            content_type="markdown",
            source_url=post.get("html_url", ""),
            created_at=self._parse_date(post.get("created_at")),
            updated_at=self._parse_date(post.get("updated_at")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "topic_id": post.get("topic_id"),
                "vote_sum": post.get("vote_sum"),
                "comment_count": post.get("comment_count"),
                "item_type": "community_post"
            }
        )
    
    async def _normalize_macro(
        self,
        macro: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize macro/canned response."""
        macro_id = macro.get("id", "")
        title = macro.get("title", "")
        
        # Extract action text
        actions = macro.get("actions", [])
        content_parts = [f"# Macro: {title}", ""]
        
        for action in actions:
            field = action.get("field", "")
            value = action.get("value", "")
            if isinstance(value, str) and value:
                content_parts.append(f"**{field}:** {value}")
        
        return NormalizedDocument(
            id=f"zd_macro_{macro_id}",
            source_id=str(macro_id),
            title=f"Macro: {title}",
            content="\n".join(content_parts),
            content_type="markdown",
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "active": macro.get("active"),
                "restriction": macro.get("restriction"),
                "item_type": "macro"
            }
        )
    
    # ===== API Methods =====
    
    async def _api_request(
        self,
        path: str,
        params: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Make Zendesk API request."""
        await self.ensure_token()
        
        url = f"{self._api_base}{path}"
        headers = {"Authorization": f"Bearer {self._token_info.access_token}"}
        
        response = await self._http_client.get(url, headers=headers, params=params)
        response.raise_for_status()
        
        return response.json()
    
    async def _api_paginate(
        self,
        path: str,
        key: str,
        params: Dict[str, Any] = None
    ) -> List[Dict[str, Any]]:
        """Paginate through Zendesk API results."""
        results = []
        params = params or {}
        
        while path:
            response = await self._api_request(path, params)
            results.extend(response.get(key, []))
            
            # Handle cursor pagination
            next_page = response.get("next_page")
            if next_page:
                path = next_page.replace(self._api_base, "")
                params = {}  # Already in URL
            else:
                path = None
        
        return results
    
    async def _get_tickets(
        self,
        since: datetime = None
    ) -> List[Dict[str, Any]]:
        """Get tickets."""
        query_parts = []
        
        if self.ticket_status:
            status_query = " ".join([f"status:{s}" for s in self.ticket_status])
            query_parts.append(f"({status_query})")
        
        if since:
            query_parts.append(f"updated>{since.strftime('%Y-%m-%d')}")
        
        query = " ".join(query_parts) if query_parts else "*"
        
        response = await self._api_request(
            "/search",
            {"query": f"type:ticket {query}", "sort_by": "updated_at"}
        )
        
        return response.get("results", [])
    
    async def _get_ticket_comments(
        self,
        ticket_id: int
    ) -> List[Dict[str, Any]]:
        """Get comments for a ticket."""
        response = await self._api_request(
            f"/tickets/{ticket_id}/comments"
        )
        return response.get("comments", [])
    
    async def _get_articles(
        self,
        since: datetime = None
    ) -> List[Dict[str, Any]]:
        """Get help center articles."""
        hc_base = f"https://{self.subdomain}.zendesk.com/api/v2/help_center"
        
        url = f"{hc_base}/articles"
        params = {"per_page": 100}
        
        if since:
            params["start_time"] = int(since.timestamp())
        
        await self.ensure_token()
        
        results = []
        while url:
            response = await self._http_client.get(
                url,
                headers={"Authorization": f"Bearer {self._token_info.access_token}"},
                params=params
            )
            response.raise_for_status()
            data = response.json()
            
            results.extend(data.get("articles", []))
            url = data.get("next_page")
            params = {}
        
        return results
    
    async def _get_community_posts(self) -> List[Dict[str, Any]]:
        """Get community posts."""
        hc_base = f"https://{self.subdomain}.zendesk.com/api/v2/community"
        
        try:
            await self.ensure_token()
            response = await self._http_client.get(
                f"{hc_base}/posts",
                headers={"Authorization": f"Bearer {self._token_info.access_token}"},
                params={"per_page": 100}
            )
            response.raise_for_status()
            return response.json().get("posts", [])
        except Exception:
            return []
    
    async def _get_macros(self) -> List[Dict[str, Any]]:
        """Get macros."""
        response = await self._api_request("/macros/active")
        return response.get("macros", [])
    
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
