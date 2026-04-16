"""
Salesforce Connector
Connects to Salesforce CRM to ingest knowledge articles, cases, and accounts.
"""
from typing import Dict, Any, List, Optional, AsyncGenerator
from datetime import datetime
import httpx
import structlog

from shared.base_connector import (
    BaseConnector, ConnectorStatus, ConnectorHealth, 
    AuthStatus, TokenInfo, NormalizedDocument, SyncResult, SyncStatus
)
from shared.oauth_handler import OAuthHandler, get_salesforce_oauth_config

logger = structlog.get_logger(__name__)


class SalesforceConnector(BaseConnector):
    """
    Salesforce Connector for Shielva.
    
    Features:
    - OAuth 2.0 authentication
    - Knowledge Article retrieval
    - Case data extraction
    - Account/Contact information
    - SOQL query support
    - Incremental sync via SystemModstamp
    """
    
    CONNECTOR_TYPE = "salesforce"
    CONNECTOR_NAME = "Salesforce"
    SUPPORTED_AUTH_TYPES = ["oauth2"]
    REQUIRED_SCOPES = [
        "api",
        "refresh_token"
    ]
    
    def __init__(
        self,
        tenant_id: str,
        connector_id: str,
        config: Dict[str, Any] = None
    ):
        """
        Initialize Salesforce connector.
        
        Config options:
        - client_id: Connected App Consumer Key
        - client_secret: Connected App Consumer Secret
        - instance_url: Salesforce instance URL (login.salesforce.com or custom domain)
        - objects: List of objects to sync (Knowledge, Case, Account, etc.)
        - soql_filters: Custom SOQL WHERE clauses per object
        - include_attachments: Include attachment metadata
        - max_records_per_object: Maximum records per object type
        """
        super().__init__(tenant_id, connector_id, config)
        
        self._http_client = httpx.AsyncClient(timeout=60.0)
        self._oauth_handler: Optional[OAuthHandler] = None
        self._instance_url: Optional[str] = None
        
        # Configuration
        self.objects = config.get("objects", ["Knowledge__kav", "Case"])
        self.soql_filters = config.get("soql_filters", {})
        self.include_attachments = config.get("include_attachments", False)
        self.max_records_per_object = config.get("max_records_per_object", 1000)
        
        # Default instance URL
        self._login_url = config.get("instance_url", "https://login.salesforce.com")
    
    # ===== Lifecycle Methods =====
    
    async def install(self) -> ConnectorStatus:
        """Install Salesforce connector."""
        logger.info("Installing Salesforce connector", tenant_id=self.tenant_id)
        
        required = ["client_id", "client_secret", "redirect_uri"]
        for key in required:
            if key not in self.config:
                self._status.health = ConnectorHealth.OFFLINE
                self._status.error = f"Missing required config: {key}"
                return self._status
        
        self._oauth_handler = OAuthHandler(
            get_salesforce_oauth_config(
                client_id=self.config["client_id"],
                client_secret=self.config["client_secret"],
                redirect_uri=self.config["redirect_uri"],
                scopes=self.REQUIRED_SCOPES,
                instance_url=self._login_url
            )
        )
        
        self._status.health = ConnectorHealth.DEGRADED
        self._status.auth_status = AuthStatus.PENDING
        
        return self._status
    
    def get_oauth_url(self, redirect_uri: str, state: str = None) -> str:
        """Get Salesforce OAuth authorization URL."""
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        return self._oauth_handler.get_authorization_url(state=state or "salesforce")
    
    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        """Complete OAuth authorization."""
        logger.info("Authorizing Salesforce connector")
        
        if not self._oauth_handler:
            raise ValueError("Connector not installed")
        
        token_info = await self._oauth_handler.exchange_code(auth_code)
        self.set_token(token_info)
        
        # Extract instance URL from token response
        self._instance_url = token_info.metadata.get("instance_url")
        
        self._status.health = ConnectorHealth.HEALTHY
        self._status.auth_status = AuthStatus.CONNECTED
        
        logger.info("Salesforce connector authorized", instance_url=self._instance_url)
        
        return token_info
    
    async def sync(
        self,
        since: datetime = None,
        full: bool = False,
        webhook_url: str = None
    ) -> SyncResult:
        """Sync data from Salesforce."""
        import uuid
        
        job_id = str(uuid.uuid4())
        result = SyncResult(
            job_id=job_id,
            status=SyncStatus.SYNCING
        )
        
        logger.info(
            "Starting Salesforce sync",
            tenant_id=self.tenant_id,
            since=since,
            full=full,
            objects=self.objects,
            webhook_url=webhook_url
        )
        
        try:
            await self.ensure_token()
            
            documents_synced = 0
            documents_failed = 0
            
            for obj_name in self.objects:
                try:
                    records = await self._query_object(obj_name, since, full)
                    
                    for record in records:
                        try:
                            doc = await self.normalize({
                                "object_type": obj_name,
                                "record": record
                            })
                            
                            # Ingest
                            success = await self.ingest_batch(self.connector_id, [doc], webhook_url=webhook_url)
                            
                            if success:
                                documents_synced += 1
                            else:
                                documents_failed += 1
                            
                            if documents_synced >= self.max_records_per_object * len(self.objects):
                                break
                                
                        except Exception as e:
                            logger.error(
                                "Failed to process record",
                                object=obj_name,
                                record_id=record.get("Id"),
                                error=str(e)
                            )
                            documents_failed += 1
                            result.errors.append(str(e))
                    
                except Exception as e:
                    logger.error(
                        "Failed to sync object",
                        object=obj_name,
                        error=str(e)
                    )
                    result.errors.append(f"{obj_name}: {str(e)}")
            
            result.documents_found = documents_synced + documents_failed
            result.documents_synced = documents_synced
            result.documents_failed = documents_failed
            result.status = SyncStatus.COMPLETED
            result.completed_at = datetime.utcnow()
            
            self._status.last_sync = datetime.utcnow()
            self._status.documents_indexed += documents_synced
            
            logger.info(
                "Salesforce sync completed",
                synced=documents_synced,
                failed=documents_failed
            )
            
        except Exception as e:
            logger.error("Salesforce sync failed", error=str(e))
            result.status = SyncStatus.FAILED
            result.errors.append(str(e))
        
        return result
    
    async def health_check(self) -> ConnectorStatus:
        """Check Salesforce connector health."""
        try:
            if not self._token_info:
                self._status.health = ConnectorHealth.OFFLINE
                return self._status
            
            if not self.is_token_valid():
                await self.on_token_refresh()
            
            # Test with limits endpoint
            await self._api_request("/services/data/v59.0/limits")
            
            self._status.health = ConnectorHealth.HEALTHY
            self._status.error = None
            
        except Exception as e:
            self._status.health = ConnectorHealth.DEGRADED
            self._status.error = str(e)
        
        return self._status
    
    async def on_token_refresh(self) -> TokenInfo:
        """Refresh Salesforce access token."""
        if not self._oauth_handler or not self._token_info:
            raise ValueError("No token to refresh")
        
        token_info = await self._oauth_handler.refresh_token(
            self._token_info.refresh_token
        )
        
        # Preserve instance URL
        token_info.metadata["instance_url"] = self._instance_url
        self.set_token(token_info)
        
        return token_info
    
    # ===== Data Methods =====
    
    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        """Normalize Salesforce record to standard format."""
        obj_type = raw_data.get("object_type", "")
        record = raw_data.get("record", {})
        
        # Route to object-specific normalizers
        if obj_type == "Knowledge__kav":
            return self._normalize_knowledge_article(record)
        elif obj_type == "Case":
            return self._normalize_case(record)
        elif obj_type == "Account":
            return self._normalize_account(record)
        elif obj_type == "Contact":
            return self._normalize_contact(record)
        else:
            return self._normalize_generic(obj_type, record)
    
    def _normalize_knowledge_article(self, record: Dict[str, Any]) -> NormalizedDocument:
        """Normalize Knowledge Article."""
        article_id = record.get("Id", "")
        title = record.get("Title", "Untitled Article")
        
        # Combine all text fields
        content_parts = [
            f"# {title}",
            "",
            record.get("Summary", ""),
            "",
            record.get("Answer__c", record.get("Content__c", "")),
        ]
        
        content = "\n".join(filter(None, content_parts))
        
        return NormalizedDocument(
            id=f"sf_knowledge_{article_id}",
            source_id=article_id,
            title=title,
            content=content,
            content_type="text",
            source_url=f"{self._instance_url}/lightning/r/Knowledge__kav/{article_id}/view",
            author=record.get("CreatedBy", {}).get("Name"),
            created_at=self._parse_date(record.get("CreatedDate")),
            updated_at=self._parse_date(record.get("LastModifiedDate")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "object_type": "Knowledge__kav",
                "article_number": record.get("ArticleNumber"),
                "article_type": record.get("RecordType", {}).get("Name"),
                "publish_status": record.get("PublishStatus"),
                "language": record.get("Language"),
                "categories": record.get("DataCategorySelections", [])
            }
        )
    
    def _normalize_case(self, record: Dict[str, Any]) -> NormalizedDocument:
        """Normalize Case record."""
        case_id = record.get("Id", "")
        case_number = record.get("CaseNumber", "")
        subject = record.get("Subject", "No Subject")
        
        content_parts = [
            f"# Case {case_number}: {subject}",
            "",
            f"**Status:** {record.get('Status', '')}",
            f"**Priority:** {record.get('Priority', '')}",
            f"**Type:** {record.get('Type', '')}",
            f"**Origin:** {record.get('Origin', '')}",
            "",
            "## Description",
            record.get("Description", ""),
            "",
            "## Resolution",
            record.get("Resolution__c", record.get("Resolution", ""))
        ]
        
        content = "\n".join(filter(None, content_parts))
        
        return NormalizedDocument(
            id=f"sf_case_{case_id}",
            source_id=case_id,
            title=f"Case {case_number}: {subject}",
            content=content,
            content_type="markdown",
            source_url=f"{self._instance_url}/lightning/r/Case/{case_id}/view",
            author=record.get("CreatedBy", {}).get("Name"),
            created_at=self._parse_date(record.get("CreatedDate")),
            updated_at=self._parse_date(record.get("LastModifiedDate")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "object_type": "Case",
                "case_number": case_number,
                "status": record.get("Status"),
                "priority": record.get("Priority"),
                "type": record.get("Type"),
                "origin": record.get("Origin"),
                "account_id": record.get("AccountId"),
                "contact_id": record.get("ContactId"),
                "is_closed": record.get("IsClosed", False)
            }
        )
    
    def _normalize_account(self, record: Dict[str, Any]) -> NormalizedDocument:
        """Normalize Account record."""
        account_id = record.get("Id", "")
        name = record.get("Name", "Unknown Account")
        
        content_parts = [
            f"# {name}",
            "",
            f"**Type:** {record.get('Type', '')}",
            f"**Industry:** {record.get('Industry', '')}",
            f"**Phone:** {record.get('Phone', '')}",
            f"**Website:** {record.get('Website', '')}",
            "",
            "## Description",
            record.get("Description", "")
        ]
        
        content = "\n".join(filter(None, content_parts))
        
        return NormalizedDocument(
            id=f"sf_account_{account_id}",
            source_id=account_id,
            title=name,
            content=content,
            content_type="markdown",
            source_url=f"{self._instance_url}/lightning/r/Account/{account_id}/view",
            created_at=self._parse_date(record.get("CreatedDate")),
            updated_at=self._parse_date(record.get("LastModifiedDate")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "object_type": "Account",
                "account_type": record.get("Type"),
                "industry": record.get("Industry"),
                "annual_revenue": record.get("AnnualRevenue"),
                "employees": record.get("NumberOfEmployees")
            }
        )
    
    def _normalize_contact(self, record: Dict[str, Any]) -> NormalizedDocument:
        """Normalize Contact record."""
        contact_id = record.get("Id", "")
        name = record.get("Name", "Unknown Contact")
        
        content_parts = [
            f"# {name}",
            "",
            f"**Title:** {record.get('Title', '')}",
            f"**Email:** {record.get('Email', '')}",
            f"**Phone:** {record.get('Phone', '')}",
            f"**Account:** {record.get('Account', {}).get('Name', '')}"
        ]
        
        content = "\n".join(filter(None, content_parts))
        
        return NormalizedDocument(
            id=f"sf_contact_{contact_id}",
            source_id=contact_id,
            title=name,
            content=content,
            content_type="markdown",
            source_url=f"{self._instance_url}/lightning/r/Contact/{contact_id}/view",
            created_at=self._parse_date(record.get("CreatedDate")),
            updated_at=self._parse_date(record.get("LastModifiedDate")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "object_type": "Contact",
                "title": record.get("Title"),
                "account_id": record.get("AccountId")
            }
        )
    
    def _normalize_generic(self, obj_type: str, record: Dict[str, Any]) -> NormalizedDocument:
        """Normalize any Salesforce object generically."""
        record_id = record.get("Id", "")
        name = record.get("Name", record.get("Subject", f"{obj_type} Record"))
        
        # Build content from all string fields
        content_parts = [f"# {name}", ""]
        
        for key, value in record.items():
            if isinstance(value, str) and value and key not in ["Id", "Name", "attributes"]:
                content_parts.append(f"**{key}:** {value}")
        
        return NormalizedDocument(
            id=f"sf_{obj_type.lower()}_{record_id}",
            source_id=record_id,
            title=name,
            content="\n".join(content_parts),
            content_type="markdown",
            source_url=f"{self._instance_url}/lightning/r/{obj_type}/{record_id}/view",
            created_at=self._parse_date(record.get("CreatedDate")),
            updated_at=self._parse_date(record.get("LastModifiedDate")),
            metadata={
                "connector_type": self.CONNECTOR_TYPE,
                "object_type": obj_type
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
        """Make authenticated API request."""
        await self.ensure_token()
        
        url = f"{self._instance_url}{path}"
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
    
    async def _query_object(
        self,
        obj_name: str,
        since: datetime = None,
        full: bool = False
    ) -> List[Dict[str, Any]]:
        """Query Salesforce object using SOQL."""
        # Get object fields
        fields = await self._get_object_fields(obj_name)
        
        # Build SOQL query
        select_fields = ", ".join(fields[:50])  # Limit fields
        query = f"SELECT {select_fields} FROM {obj_name}"
        
        # Add WHERE clause
        where_parts = []
        
        if since and not full:
            date_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            where_parts.append(f"SystemModstamp > {date_str}")
        
        if obj_name in self.soql_filters:
            where_parts.append(f"({self.soql_filters[obj_name]})")
        
        if where_parts:
            query += " WHERE " + " AND ".join(where_parts)
        
        query += f" ORDER BY SystemModstamp DESC LIMIT {self.max_records_per_object}"
        
        # Execute query
        path = f"/services/data/v59.0/query?q={query}"
        response = await self._api_request(path)
        
        all_records = response.get("records", [])
        
        # Handle pagination
        while response.get("nextRecordsUrl"):
            response = await self._api_request(response["nextRecordsUrl"])
            all_records.extend(response.get("records", []))
            
            if len(all_records) >= self.max_records_per_object:
                break
        
        return all_records[:self.max_records_per_object]
    
    async def _get_object_fields(self, obj_name: str) -> List[str]:
        """Get queryable fields for an object."""
        try:
            path = f"/services/data/v59.0/sobjects/{obj_name}/describe"
            response = await self._api_request(path)
            
            # Get text fields that are queryable
            fields = []
            for field in response.get("fields", []):
                if field.get("type") in ["string", "textarea", "picklist", "id", "datetime", "reference"]:
                    if field.get("name") not in ["attributes"]:
                        fields.append(field["name"])
            
            return fields
            
        except Exception:
            # Fallback to common fields
            return ["Id", "Name", "CreatedDate", "LastModifiedDate"]
    
    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse Salesforce date string."""
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
