"""
HubSpot Connector
Connects to HubSpot to ingest CRM data, knowledge base, and content.
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


def get_hubspot_oauth_config(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: List[str]
) -> OAuthConfig:
    """Get HubSpot OAuth configuration."""
    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        authorization_url="https://app.hubspot.com/oauth/authorize",
        token_url="https://api.hubapi.com/oauth/v1/token",
        scopes=scopes,
        redirect_uri=redirect_uri
    )


class HubSpotConnector(BaseConnector):
    """
    HubSpot Connector for Shielva.
    
    Features:
    - OAuth 2.0 authentication
    - Knowledge base articles
    - Blog posts
    - Contact and company records
    - Deals and tickets
    - Email templates
    - Landing pages
    """
    
    CONNECTOR_TYPE = "hubspot"
    CONNECTOR_NAME = "HubSpot"
    SUPPORTED_AUTH_TYPES = ["oauth2"]
    REQUIRED_SCOPES = [
        "crm.objects.contacts.read",
        "crm.objects.companies.read",
        "crm.objects.deals.read",
        "content"
    ]
    
    API_BASE = "https://api.hubapi.com"
    
    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None
    ):
        """
        Initialize HubSpot connector.
        
        Config options:
        - client_id: HubSpot OAuth Client ID
        - client_secret: HubSpot OAuth Client Secret
        - include_kb: Include knowledge base articles
        - include_blogs: Include blog posts
        - include_contacts: Include contacts (for CRM tool)
        - include_companies: Include companies
        - include_deals: Include deals
        - include_tickets: Include tickets
        - include_emails: Include email templates
        """
        super().__init__(tenant_id, connector_id, config)
        
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self._oauth_handler: Optional[OAuthHandler] = None
        self._portal_id: Optional[str] = None
        
        # Configuration
        self.include_kb = config.get("include_kb", True)
        self.include_blogs = config.get("include_blogs", True)
        self.include_contacts = config.get("include_contacts", False)
        self.include_companies = config.get("include_companies", False)
        self.include_deals = config.get("include_deals", False)
        self.include_tickets = config.get("include_tickets", True)
        self.include_emails = config.get("include_emails", False)
    
    # ===== Lifecycle Methods =====
    
    async def install(self) -> ConnectorStatus:
        """Install HubSpot connector."""
        logger.info("Installing HubSpot connector", tenant_id=self.tenant_id)
        
        required = ["client_id", "client_secret", "redirect_uri"]
        for key in required:
            if key not in self.config:
                self._status.health = ConnectorHealth.OFFLINE
                self._status.error = f"Missing required config: {key}"
                return self._status
        
        self._oauth_handler = OAuthHandler(
            get_hubspot_oauth_config(
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
        """Get HubSpot OAuth authorization URL."""
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        return self._oauth_handler.get_authorization_url(state=state or "hubspot")
    
    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Complete OAuth authorization."""
        logger.info("Authorizing HubSpot connector")
        
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        token_info = await self._oauth_handler.exchange_code(auth_code)
        self.set_token(token_info)
        
        # Get portal ID
        account_info = await self._api_request("/account-info/v3/details")
        self._portal_id = account_info.get("portalId")
        
        self._status.health = ConnectorHealth.HEALTHY
        self._status.auth_status = AuthStatus.CONNECTED
        
        return token_info
    
    async def sync(
        self,
        since: datetime = None,
        full: bool = False
    ) -> SyncResult:
        """Sync content from HubSpot."""
        import uuid
        
        job_id = str(uuid.uuid4())
        result = SyncResult(
            job_id=job_id,
            status=SyncStatus.SYNCING
        )
        
        logger.info(
            "Starting HubSpot sync",
            tenant_id=self.tenant_id,
            portal_id=self._portal_id
        )
        
        try:
            await self.ensure_token()
            
            documents_synced = 0
            documents_failed = 0
            
            # Sync knowledge base articles
            if self.include_kb:
                articles = await self._get_kb_articles(since if not full else None)
                for article in articles:
                    try:
                        doc = await self.normalize(article)
                        documents_synced += 1
                    except Exception as e:
                        documents_failed += 1
                        result.errors.append(str(e))
            
            # Sync blog posts
            if self.include_blogs:
                posts = await self._get_blog_posts(since if not full else None)
                for post in posts:
                    try:
                        doc = await self._normalize_blog_post(post)
                        documents_synced += 1
                    except Exception as e:
                        documents_failed += 1
            
            # Sync tickets
            if self.include_tickets:
                tickets = await self._get_tickets(since if not full else None)
                for ticket in tickets:
                    try:
                        doc = await self._normalize_ticket(ticket)
                        documents_synced += 1
                    except Exception as e:
                        documents_failed += 1
            
            # Sync contacts (limited for privacy)
            if self.include_contacts:
                contacts = await self._get_contacts()
                for contact in contacts:
                    try:
                        doc = await self._normalize_contact(contact)
                        documents_synced += 1
                    except Exception as e:
                        documents_failed += 1
            
            # Sync companies
            if self.include_companies:
                companies = await self._get_companies()
                for company in companies:
                    try:
                        doc = await self._normalize_company(company)
                        documents_synced += 1
                    except Exception as e:
                        documents_failed += 1
            
            # Sync deals
            if self.include_deals:
                deals = await self._get_deals()
                for deal in deals:
                    try:
                        doc = await self._normalize_deal(deal)
                        documents_synced += 1
                    except Exception as e:
                        documents_failed += 1
            
            # Sync email templates
            if self.include_emails:
                templates = await self._get_email_templates()
                for template in templates:
                    try:
                        doc = await self._normalize_email_template(template)
                        documents_synced += 1
                    except Exception as e:
                        documents_failed += 1
            
            result.documents_found = documents_synced + documents_failed
            result.documents_synced = documents_synced
            result.documents_failed = documents_failed
            result.status = SyncStatus.COMPLETED
            result.completed_at = datetime.utcnow()
            
            self._status.last_sync = datetime.utcnow()
            self._status.documents_indexed += documents_synced
            
            logger.info(
                "HubSpot sync completed",
                synced=documents_synced,
                failed=documents_failed
            )
            
        except Exception as e:
            logger.error("HubSpot sync failed", error=str(e))
            result.status = SyncStatus.FAILED
            result.errors.append(str(e))
        
        return result
    
    async def health_check(self) -> ConnectorStatus:
        """Check HubSpot connector health."""
        try:
            if not self._token_info:
                self._status.health = ConnectorHealth.OFFLINE
                return self._status
            
            if not self.is_token_valid():
                await self.on_token_refresh()
            
            # Test with account info
            await self._api_request("/account-info/v3/details")
            
            self._status.health = ConnectorHealth.HEALTHY
            self._status.error = None
            
        except Exception as e:
            self._status.health = ConnectorHealth.DEGRADED
            self._status.error = str(e)
        
        return self._status
    
    async def on_token_refresh(self) -> TokenInfo:
        """Refresh HubSpot access token."""
        if not self._oauth_handler or not self._token_info:
            raise ValueError("No token to refresh")
        
        token_info = await self._oauth_handler.refresh_token(
            self._token_info.refresh_token
        )
        self.set_token(token_info)
        
        return token_info
    
    # ===== Data Methods =====
    
    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        """Normalize HubSpot knowledge base article."""
        article_id = raw_data.get("id", "")
        title = raw_data.get("title", "")
        
        # Get HTML content and strip tags
        html_body = raw_data.get("htmlBody", raw_data.get("body", ""))
        import re
        clean_body = re.sub(r'<[^>]+>', '', html_body)
        
        return NormalizedDocument(
            id=f"hs_kb_{article_id}",
            source_id=str(article_id),
            title=title,
            content=f"# {title}\n\n{clean_body}",
            content_type="markdown",
            source_url=raw_data.get("url", ""),
            author=raw_data.get("authorName"),
            created_at=self._parse_timestamp(raw_data.get("createdAt")),
            updated_at=self._parse_timestamp(raw_data.get("updatedAt")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "category": raw_data.get("category"),
                "subcategory": raw_data.get("subcategory"),
                "state": raw_data.get("state"),
                "item_type": "kb_article"
            }
        )
    
    async def _normalize_blog_post(
        self,
        post: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize blog post."""
        post_id = post.get("id", "")
        title = post.get("name", post.get("title", ""))
        
        # Get content
        html_body = post.get("postBody", post.get("body", ""))
        import re
        clean_body = re.sub(r'<[^>]+>', '', html_body)
        
        return NormalizedDocument(
            id=f"hs_blog_{post_id}",
            source_id=str(post_id),
            title=title,
            content=f"# {title}\n\n{clean_body}",
            content_type="markdown",
            source_url=post.get("url", ""),
            author=post.get("authorName"),
            created_at=self._parse_timestamp(post.get("createdAt")),
            updated_at=self._parse_timestamp(post.get("updatedAt")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "blog_id": post.get("blogId"),
                "state": post.get("state"),
                "tags": post.get("tagIds", []),
                "item_type": "blog_post"
            }
        )
    
    async def _normalize_ticket(
        self,
        ticket: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize HubSpot ticket."""
        ticket_id = ticket.get("id", "")
        properties = ticket.get("properties", {})
        
        subject = properties.get("subject", "")
        content = properties.get("content", "")
        
        content_parts = [
            f"# Ticket: {subject}",
            "",
            f"**Status:** {properties.get('hs_pipeline_stage')}",
            f"**Priority:** {properties.get('hs_ticket_priority')}",
            f"**Category:** {properties.get('hs_ticket_category')}",
            "",
            "## Content",
            content
        ]
        
        return NormalizedDocument(
            id=f"hs_ticket_{ticket_id}",
            source_id=str(ticket_id),
            title=f"Ticket: {subject}",
            content="\n".join(content_parts),
            content_type="markdown",
            created_at=self._parse_timestamp(properties.get("createdate")),
            updated_at=self._parse_timestamp(properties.get("hs_lastmodifieddate")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "pipeline": properties.get("hs_pipeline"),
                "stage": properties.get("hs_pipeline_stage"),
                "priority": properties.get("hs_ticket_priority"),
                "item_type": "ticket"
            }
        )
    
    async def _normalize_contact(
        self,
        contact: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize contact."""
        contact_id = contact.get("id", "")
        properties = contact.get("properties", {})
        
        name = f"{properties.get('firstname', '')} {properties.get('lastname', '')}".strip()
        email = properties.get("email", "")
        company = properties.get("company", "")
        
        content_parts = [
            f"# Contact: {name or email}",
            "",
            f"**Email:** {email}",
            f"**Company:** {company}",
            f"**Phone:** {properties.get('phone', '')}",
            f"**Job Title:** {properties.get('jobtitle', '')}",
            f"**Lifecycle Stage:** {properties.get('lifecyclestage', '')}"
        ]
        
        return NormalizedDocument(
            id=f"hs_contact_{contact_id}",
            source_id=str(contact_id),
            title=f"Contact: {name or email}",
            content="\n".join(content_parts),
            content_type="markdown",
            created_at=self._parse_timestamp(properties.get("createdate")),
            updated_at=self._parse_timestamp(properties.get("lastmodifieddate")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "email": email,
                "company": company,
                "lifecycle_stage": properties.get("lifecyclestage"),
                "item_type": "contact"
            }
        )
    
    async def _normalize_company(
        self,
        company: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize company."""
        company_id = company.get("id", "")
        properties = company.get("properties", {})
        
        name = properties.get("name", "")
        domain = properties.get("domain", "")
        
        content_parts = [
            f"# Company: {name}",
            "",
            f"**Domain:** {domain}",
            f"**Industry:** {properties.get('industry', '')}",
            f"**Type:** {properties.get('type', '')}",
            f"**Phone:** {properties.get('phone', '')}",
            f"**City:** {properties.get('city', '')}",
            f"**Country:** {properties.get('country', '')}",
            "",
            "## Description",
            properties.get("description", "")
        ]
        
        return NormalizedDocument(
            id=f"hs_company_{company_id}",
            source_id=str(company_id),
            title=f"Company: {name}",
            content="\n".join(content_parts),
            content_type="markdown",
            source_url=f"https://{domain}" if domain else None,
            created_at=self._parse_timestamp(properties.get("createdate")),
            updated_at=self._parse_timestamp(properties.get("hs_lastmodifieddate")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "domain": domain,
                "industry": properties.get("industry"),
                "item_type": "company"
            }
        )
    
    async def _normalize_deal(
        self,
        deal: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize deal."""
        deal_id = deal.get("id", "")
        properties = deal.get("properties", {})
        
        name = properties.get("dealname", "")
        amount = properties.get("amount", "")
        
        content_parts = [
            f"# Deal: {name}",
            "",
            f"**Amount:** ${amount}",
            f"**Stage:** {properties.get('dealstage', '')}",
            f"**Pipeline:** {properties.get('pipeline', '')}",
            f"**Close Date:** {properties.get('closedate', '')}",
            "",
            "## Description",
            properties.get("description", "")
        ]
        
        return NormalizedDocument(
            id=f"hs_deal_{deal_id}",
            source_id=str(deal_id),
            title=f"Deal: {name}",
            content="\n".join(content_parts),
            content_type="markdown",
            created_at=self._parse_timestamp(properties.get("createdate")),
            updated_at=self._parse_timestamp(properties.get("hs_lastmodifieddate")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "amount": amount,
                "stage": properties.get("dealstage"),
                "pipeline": properties.get("pipeline"),
                "item_type": "deal"
            }
        )
    
    async def _normalize_email_template(
        self,
        template: Dict[str, Any]
    ) -> NormalizedDocument:
        """Normalize email template."""
        template_id = template.get("id", "")
        name = template.get("name", "")
        
        subject = template.get("subject", "")
        body = template.get("body", "")
        
        import re
        clean_body = re.sub(r'<[^>]+>', '', body)
        
        return NormalizedDocument(
            id=f"hs_email_{template_id}",
            source_id=str(template_id),
            title=f"Email Template: {name}",
            content=f"# {name}\n\n**Subject:** {subject}\n\n{clean_body}",
            content_type="markdown",
            created_at=self._parse_timestamp(template.get("createdAt")),
            updated_at=self._parse_timestamp(template.get("updatedAt")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "folder_id": template.get("folderId"),
                "item_type": "email_template"
            }
        )
    
    # ===== API Methods =====
    
    async def _api_request(
        self,
        path: str,
        method: str = "GET",
        params: Dict[str, Any] = None,
        json: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Make HubSpot API request."""
        await self.ensure_token()
        
        url = f"{self.API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self._token_info.access_token}",
            "Content-Type": "application/json"
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
    
    async def _get_kb_articles(
        self,
        since: datetime = None
    ) -> List[Dict[str, Any]]:
        """Get knowledge base articles."""
        try:
            response = await self._api_request("/cms/v3/blogs/posts")
            articles = response.get("results", [])
            
            if since:
                articles = [
                    a for a in articles
                    if self._parse_timestamp(a.get("updatedAt")) > since
                ]
            
            return articles
        except Exception:
            # KB might not be available
            return []
    
    async def _get_blog_posts(
        self,
        since: datetime = None
    ) -> List[Dict[str, Any]]:
        """Get blog posts."""
        try:
            response = await self._api_request("/cms/v3/blogs/posts")
            posts = response.get("results", [])
            
            if since:
                posts = [
                    p for p in posts
                    if self._parse_timestamp(p.get("updatedAt")) > since
                ]
            
            return posts
        except Exception:
            return []
    
    async def _get_tickets(
        self,
        since: datetime = None
    ) -> List[Dict[str, Any]]:
        """Get tickets."""
        try:
            properties = [
                "subject", "content", "hs_pipeline", "hs_pipeline_stage",
                "hs_ticket_priority", "hs_ticket_category", "createdate",
                "hs_lastmodifieddate"
            ]
            
            body = {
                "properties": properties,
                "limit": 100
            }
            
            if since:
                body["filterGroups"] = [{
                    "filters": [{
                        "propertyName": "hs_lastmodifieddate",
                        "operator": "GT",
                        "value": str(int(since.timestamp() * 1000))
                    }]
                }]
            
            response = await self._api_request(
                "/crm/v3/objects/tickets/search",
                method="POST",
                json=body
            )
            return response.get("results", [])
        except Exception:
            return []
    
    async def _get_contacts(self) -> List[Dict[str, Any]]:
        """Get contacts."""
        try:
            properties = [
                "firstname", "lastname", "email", "company", "phone",
                "jobtitle", "lifecyclestage", "createdate", "lastmodifieddate"
            ]
            
            response = await self._api_request(
                "/crm/v3/objects/contacts",
                params={"properties": ",".join(properties), "limit": 100}
            )
            return response.get("results", [])
        except Exception:
            return []
    
    async def _get_companies(self) -> List[Dict[str, Any]]:
        """Get companies."""
        try:
            properties = [
                "name", "domain", "industry", "type", "phone", "city",
                "country", "description", "createdate", "hs_lastmodifieddate"
            ]
            
            response = await self._api_request(
                "/crm/v3/objects/companies",
                params={"properties": ",".join(properties), "limit": 100}
            )
            return response.get("results", [])
        except Exception:
            return []
    
    async def _get_deals(self) -> List[Dict[str, Any]]:
        """Get deals."""
        try:
            properties = [
                "dealname", "amount", "dealstage", "pipeline", "closedate",
                "description", "createdate", "hs_lastmodifieddate"
            ]
            
            response = await self._api_request(
                "/crm/v3/objects/deals",
                params={"properties": ",".join(properties), "limit": 100}
            )
            return response.get("results", [])
        except Exception:
            return []
    
    async def _get_email_templates(self) -> List[Dict[str, Any]]:
        """Get email templates."""
        try:
            response = await self._api_request("/marketing-emails/v1/emails")
            return response.get("objects", [])
        except Exception:
            return []
    
    def _parse_timestamp(self, timestamp: Any) -> Optional[datetime]:
        """Parse HubSpot timestamp (milliseconds or ISO string)."""
        if not timestamp:
            return None
        
        try:
            if isinstance(timestamp, (int, float)):
                return datetime.fromtimestamp(timestamp / 1000)
            elif isinstance(timestamp, str):
                if timestamp.isdigit():
                    return datetime.fromtimestamp(int(timestamp) / 1000)
                return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except (ValueError, OSError):
            return None
        
        return None
    
    async def close(self):
        """Close HTTP clients."""
        await self._http_client.aclose()
        if self._oauth_handler:
            await self._oauth_handler.close()
