"""Integration Builder — MongoDB async driver (Motor)."""

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from integration.core.config import settings
import structlog
try:
    import certifi as _certifi; _TLS_CA = _certifi.where()
except ImportError:
    _TLS_CA = None

def _make_client(url: str) -> AsyncIOMotorClient:
    kw = {"tlsCAFile": _TLS_CA} if _TLS_CA and url.startswith("mongodb+srv") else {}
    return AsyncIOMotorClient(url, **kw)

logger = structlog.get_logger(__name__)

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def connect_db() -> None:
    global _client, _db
    _client = _make_client(settings.MONGODB_URL)
    _db = _client[settings.MONGODB_DB]
    logger.info("mongodb.connected", db=settings.MONGODB_DB)


async def close_db() -> None:
    global _client
    if _client:
        _client.close()
        logger.info("mongodb.disconnected")


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Database not initialised — call connect_db() first")
    return _db


# ── Collection accessors ─────────────────────────────────────────────

def sessions_collection():
    """IntegrationSession documents."""
    return get_db()["integration_sessions"]


def custom_providers_collection():
    """CustomProvider documents — user-defined providers + services."""
    return get_db()["custom_providers"]


def static_provider_overrides_collection():
    """Overrides for built-in static providers (super-admin edits)."""
    return get_db()["static_provider_overrides"]
