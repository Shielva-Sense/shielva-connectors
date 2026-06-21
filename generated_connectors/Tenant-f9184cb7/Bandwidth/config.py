"""Bandwidth Connector — Configuration.

Credentials (account_id, username, password) are NOT defined here.
They are supplied at install time via install_fields and injected into
self.config by the gateway — never hardcode them.
"""

from pydantic_settings import BaseSettings


class BandwidthConfig(BaseSettings):
    """Static, non-secret runtime configuration for the Bandwidth connector."""

    TIMEOUT_S: float = 60.0

    model_config = {"env_prefix": "BANDWIDTH_", "env_file": ".env", "extra": "ignore"}


config = BandwidthConfig()
