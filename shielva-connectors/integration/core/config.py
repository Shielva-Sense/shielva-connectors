"""Integration Builder — Configuration."""

from pydantic_settings import BaseSettings
from typing import Optional


class IntegrationSettings(BaseSettings):
    """Settings loaded from environment variables."""

    # MongoDB
    MONGODB_URL: str = "mongodb+srv://shielvaadmin:shielvaadmin123@mastershielva.8rbs44q.mongodb.net/?appName=MasterShielva"
    MONGODB_DB: str = "ShielvaIntegration"

    # Service
    INTEGRATION_PORT: int = 8055
    SERVICE_NAME: str = "integration-builder"

    # API Gateway (for service registration)
    API_GATEWAY_URL: str = "https://localhost:8000"

    # LLM — Claude as primary
    # Mode: "cli"    = use Claude CLI directly (local dev, Max plan, $0)
    #        "worker" = push jobs to Redis, worker machine calls Claude CLI ($0)
    #        "api"    = use Anthropic API directly (requires ANTHROPIC_API_KEY, $$)
    LLM_MODE: str = "cli"
    ANTHROPIC_API_KEY: str = ""
    LLM_MODEL: str = "claude-sonnet-4-20250514"
    LLM_MAX_TOKENS: int = 8192
    CLAUDE_CLI_PATH: str = "/opt/homebrew/Caskroom/claude-code/2.1.59/claude"  # path to claude CLI binary

    # Test LLM — separate model for test generation (faster + cheaper than Claude CLI)
    # TEST_LLM_MODE options: "gemini" | "kimi" | "" (empty = use default Claude LLM)
    TEST_LLM_MODE: str = ""

    # Google Gemini — connector generation + test generation
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"          # connector agentic generation
    TEST_GEMINI_MODEL: str = "gemini-2.5-flash"    # test generation (thinking-capable)
    # Thinking budget for Gemini — applies to test generation and fix calls.
    # -1 = dynamic (model decides), 0 = disabled, N = max thinking tokens.
    GEMINI_THINKING_BUDGET: int = -1  # -1 = dynamic thinking for test gen/fix

    # Kimi (Moonshot AI) — alternative for test generation (~20-40s, cheap)
    KIMI_API_KEY: str = ""
    KIMI_MODEL: str = "moonshot-v1-32k"
    KIMI_BASE_URL: str = "https://api.moonshot.cn/v1"

    # Redis (used by worker mode + rate limiting)
    REDIS_URL: str = "redis://localhost:6379"
    LLM_WORKER_TIMEOUT: int = 300  # seconds to wait for worker result

    # Generated code output directory
    GENERATED_CODE_DIR: str = "./generated_connectors"

    # Cloudflare R2 (for caching integration prompts and plans)
    # Two-bucket architecture:
    #   1. R2_SHARED_BUCKET  — shared read-only bucket for global resources:
    #                           guidelines, step prompts, docs templates.
    #                           Path: shielvasense / shielvasense-integration-plans/
    #   2. Per-app bucket    — derived from X-App-ID: "shielva-agentic-app-{app_id}"
    #                           Used for all connector-specific data:
    #                           plan.json, progress.json, generated code, etc.
    # Leave R2_ACCOUNT_ID empty to disable R2 caching (local filesystem fallback).
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    # R2_SHARED_BUCKET — fixed shared bucket holding guidelines + step prompts.
    # This bucket is the same for every installation and is managed by Shielva admins.
    R2_SHARED_BUCKET: str = "shielvasense"
    # R2_BUCKET_NAME — kept for backward compat / startup-time bucket check.
    # At runtime the per-app bucket is derived from X-App-ID (see r2_service._get_bucket).
    R2_BUCKET_NAME: str = ""
    R2_COLLECTION_PREFIX: str = "shielvasense-integration-plans"  # key prefix inside bucket

    # shielva-mcp (used when LLM_MODE=mcp)
    # Set INTEGRATION_MCP_URL in .env to point at the running MCP server.
    MCP_URL: str = "http://localhost:8004"

    # MCP ingestion worker (used for RAG knowledge upload)
    MCP_INGESTION_URL: str = "http://localhost:8007"

    # Credential encryption — server-side HMAC secret for local credential files.
    # Frontend mixes this HMAC into AES-256-GCM key derivation so credentials
    # cannot be decrypted without the backend's secret.
    CREDENTIAL_SECRET: str = "shielva-cred-secret-change-me"

    # Shielva Security platform (SDK integration)
    # Set INTEGRATION_SHIELVA_SECURITY_API_KEY to enable enhanced scanning
    # via the Shielva Security platform alongside local pip-audit + Semgrep.
    SHIELVA_SECURITY_URL: str = "https://localhost:8045"
    SHIELVA_SECURITY_API_KEY: str = ""
    # Timeout in seconds to wait for a Shielva Security scan to complete
    SHIELVA_SECURITY_SCAN_TIMEOUT: int = 300

    # Connector gateway URL (for hot-reload after sync request merge)
    CONNECTOR_GATEWAY_URL: str = "https://localhost:8003"

    # GitHub webhook secret for sync request HMAC verification (fail-closed: empty = reject all)
    GITHUB_WEBHOOK_SECRET: str = ""

    # Fernet encryption key for GitHub PAT at rest in MongoDB.
    # Can be a 32-byte url-safe base64 key (44 chars ending in =) or any passphrase
    # (will be SHA-256 hashed into a valid Fernet key). Empty = store tokens in plain text.
    SYNC_TOKEN_ENCRYPTION_KEY: str = ""

    # SSL
    SSL_CERTFILE: Optional[str] = None
    SSL_KEYFILE: Optional[str] = None

    # CORS origins (inherit from common or explicit)
    CORS_ORIGINS: list[str] = [
        "https://localhost:3000",
        "https://localhost:3002",
        "https://localhost:3005",
        "https://localhost:3010",
        "https://localhost:8000",
        "http://localhost:3000",
        "http://localhost:3005",
    ]

    model_config = {"env_prefix": "INTEGRATION_", "env_file": (".env", "integration/.env"), "extra": "ignore"}


settings = IntegrationSettings()
