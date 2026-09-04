"""Connector install config is sealed at rest — and old rows still read.

Whatever a customer types into an install form (client_secret, api_key,
bot_token) is persisted as the connector's config. That payload used to go to
Mongo AND to Redis as cleartext JSON while OAuth tokens beside it were already
encrypted, so the credential that mints the token was readable and the token
was not.

Two properties are worth pinning, because getting either wrong is silent:

  * the sealed/cleartext discriminator — cleartext is always the serialised
    model, so it always starts with "{". If `_is_sealed` ever said True for
    cleartext, a pre-sealing row would be handed to the decryptor and read as
    missing: the connector would look uninstalled and be silently reinstalled.
  * the legacy read path — rows written before sealing must keep working
    untouched, or the change is a data-loss migration wearing a fix's clothes.

Imported from services/ directly rather than through gateway: gateway pulls in
FastAPI, redis and motor, and CI's coverage stage installs only pytest.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.connector_store import ConnectorStore

CLEARTEXT = '{"connector_id":"c1","tenant_id":"Tenant-1","config":{"client_secret":"s3cr3t"}}'
SEALED = "v1:Gi9iB4kKgtpbPnbOFbYJKS19O4190pJpGE0VApSH9yZRskYjhOuoujwld24gMs4="


@pytest.mark.parametrize(
    ("value", "sealed"),
    [
        (CLEARTEXT, False),
        ("   " + CLEARTEXT, False),  # whitespace must not flip the verdict
        (SEALED, True),
        ("", False),
        (None, False),
    ],
)
def test_sealed_discriminator(value, sealed):
    assert ConnectorStore._is_sealed(value) is sealed


@pytest.mark.asyncio
async def test_legacy_cleartext_row_still_reads():
    """A row written before sealing is returned as-is, not sent to the decryptor."""
    store = ConnectorStore()
    out = await store._open_config(CLEARTEXT, "Tenant-1", "c1")
    assert out == CLEARTEXT


@pytest.mark.asyncio
async def test_seal_refuses_cleartext_fallback_without_encryption(monkeypatch):
    """No encryptor must fail the install, not persist the credentials readable."""
    monkeypatch.setattr("services.connector_store._encryptor", lambda: None)
    store = ConnectorStore()
    with pytest.raises(RuntimeError, match="cleartext"):
        await store._seal_config("c1", "Tenant-1", CLEARTEXT)
