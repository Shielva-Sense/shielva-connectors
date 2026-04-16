# Shielva Connectors

Enterprise-grade data connectors for the Shielva ARC platform. Each connector integrates with external data sources and normalizes data for ingestion into the RAG pipeline.

## Architecture

```
Shielva Connectors/
├── shared/                    # Shared utilities
│   ├── base_connector.py      # Abstract base class
│   ├── oauth_handler.py       # OAuth 2.0 implementation
│   ├── rate_limiter.py        # API rate limiting
│   └── normalizer.py          # Data normalization
│
├── gateway.py                 # Central gateway service
│
├── confluence/                # Atlassian Confluence
├── gdrive/                    # Google Drive
├── slack/                     # Slack
├── jira/                      # Atlassian Jira
├── salesforce/                # Salesforce CRM
├── github/                    # GitHub
├── notion/                    # Notion
├── sharepoint/                # Microsoft SharePoint
├── teams/                     # Microsoft Teams
├── zendesk/                   # Zendesk
└── [20+ more connectors...]
```

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Run Gateway Service

```bash
python gateway.py
```

The gateway runs on `http://localhost:8003`

### 3. Install a Connector

```bash
curl -X POST http://localhost:8003/connectors/confluence/install \
  -H "X-Tenant-ID: tenant-123" \
  -H "Content-Type: application/json" \
  -d '{
    "connector_type": "confluence",
    "config": {
      "client_id": "your-oauth-client-id",
      "client_secret": "your-oauth-secret",
      "redirect_uri": "http://localhost:3000/callback"
    }
  }'
```

Response:
```json
{
  "connector_id": "confluence_tenant-123_abc12345",
  "connector_type": "confluence",
  "status": "pending",
  "oauth_url": "https://auth.atlassian.com/authorize?..."
}
```

### 4. Complete OAuth

Redirect user to `oauth_url`, then handle callback:

```bash
curl -X POST http://localhost:8003/connectors/{connector_id}/callback \
  -H "X-Tenant-ID: tenant-123" \
  -d '{"code": "oauth-authorization-code"}'
```

### 5. Trigger Sync

```bash
curl -X POST http://localhost:8003/connectors/{connector_id}/sync \
  -H "X-Tenant-ID: tenant-123" \
  -d '{"full_sync": true}'
```

## Creating a New Connector

### 1. Create Connector Directory

```
connectors/
└── my_service/
    ├── __init__.py
    ├── connector.py      # Main connector class
    ├── api_client.py     # API wrapper (optional)
    ├── models.py         # Data models (optional)
    └── config.py         # Configuration (optional)
```

### 2. Implement Base Connector

```python
from shared.base_connector import BaseConnector, NormalizedDocument

class MyServiceConnector(BaseConnector):
    CONNECTOR_TYPE = "my_service"
    CONNECTOR_NAME = "My Service"
    REQUIRED_SCOPES = ["read:data"]
    
    async def install(self) -> ConnectorStatus:
        # Setup connector
        pass
    
    async def authorize(self, auth_code: str, state: str = None) -> TokenInfo:
        # Complete OAuth
        pass
    
    async def sync(self, since: datetime = None, full: bool = False) -> SyncResult:
        # Fetch and sync data
        pass
    
    async def health_check(self) -> ConnectorStatus:
        # Check connector health
        pass
    
    async def normalize(self, raw_data: Any) -> NormalizedDocument:
        # Normalize to standard format
        return NormalizedDocument(
            id=f"my_service_{raw_data['id']}",
            source_id=raw_data['id'],
            title=raw_data['title'],
            content=raw_data['content'],
            content_type="text",
            metadata={...}
        )
```

### 3. Register in Gateway

Add to `CONNECTOR_CLASSES` in `gateway.py`:

```python
from my_service.connector import MyServiceConnector

CONNECTOR_CLASSES = {
    ...
    "my_service": MyServiceConnector,
}
```

## Supported Connectors

### Currently Implemented

| Connector | Type | Auth | Status |
|-----------|------|------|--------|
| Confluence | `confluence` | OAuth 2.0 | ✅ Ready |
| Google Drive | `gdrive` | OAuth 2.0 | ✅ Ready |

### Coming Soon

| Connector | Type | Auth | Priority |
|-----------|------|------|----------|
| Slack | `slack` | OAuth 2.0 | High |
| Jira | `jira` | OAuth 2.0 | High |
| Salesforce | `salesforce` | OAuth 2.0 | High |
| GitHub | `github` | OAuth 2.0 | Medium |
| SharePoint | `sharepoint` | OAuth 2.0 | Medium |
| Notion | `notion` | OAuth 2.0 | Medium |
| Teams | `teams` | OAuth 2.0 | Medium |
| Zendesk | `zendesk` | OAuth 2.0 | Low |
| HubSpot | `hubspot` | OAuth 2.0 | Low |

## API Reference

### Gateway Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/connectors/types` | List available types |
| POST | `/connectors/{type}/install` | Install connector |
| POST | `/connectors/{id}/callback` | OAuth callback |
| POST | `/connectors/{id}/sync` | Trigger sync |
| GET | `/connectors/{id}/status` | Get status |
| GET | `/connectors` | List all connectors |
| DELETE | `/connectors/{id}` | Delete connector |
| POST | `/webhooks/{type}/{id}` | Webhook handler |

### NormalizedDocument Schema

All connectors normalize data to this format:

```python
{
    "id": "confluence_12345",           # Unique ID
    "source_id": "12345",               # ID in source system
    "title": "Document Title",           # Document title
    "content": "Full text content...",   # Extracted text
    "content_type": "text",              # text, html, markdown
    "source_url": "https://...",         # Link to source
    "author": "John Doe",                # Author name
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-15T00:00:00Z",
    "metadata": {                         # Connector-specific
        "connector_type": "confluence",
        "space_key": "DOCS",
        ...
    }
}
```

## Security

### OAuth Token Storage

- Tokens should be stored encrypted in a secrets manager (Vault, KMS)
- Never log tokens
- Implement token refresh before expiry

### Tenant Isolation

- All requests require `X-Tenant-ID` header
- Connectors are scoped to tenant
- Cross-tenant access is denied

### Rate Limiting

Use the `RateLimiter` utility to respect API limits:

```python
from shared.rate_limiter import RateLimiter

limiter = RateLimiter(max_requests=100, window_seconds=60)
await limiter.acquire()
# Make API call
```

## Testing

```bash
# Run tests
pytest tests/

# Test specific connector
pytest tests/test_confluence.py -v
```

## Environment Variables

```bash
# OAuth Credentials (per provider)
CONFLUENCE_CLIENT_ID=xxx
CONFLUENCE_CLIENT_SECRET=xxx
GDRIVE_CLIENT_ID=xxx
GDRIVE_CLIENT_SECRET=xxx

# Gateway
CONNECTOR_GATEWAY_PORT=8003
CONNECTOR_GATEWAY_HOST=0.0.0.0

# Vault (optional)
VAULT_URL=http://localhost:8200
VAULT_TOKEN=xxx
```
