"""
OAuth Handler - Generic OAuth 2.0 flow handler for connectors
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from .base_connector import TokenInfo

logger = structlog.get_logger(__name__)


@dataclass
class OAuthConfig:
    """OAuth configuration"""

    client_id: str
    client_secret: str
    authorization_url: str
    token_url: str
    scopes: list[str]
    redirect_uri: str

    # Optional provider-specific
    audience: str | None = None
    extra_params: dict[str, str] = None


class OAuthHandler:
    """
    Generic OAuth 2.0 handler.

    Supports:
    - Authorization Code flow
    - Token refresh
    - PKCE (optional)
    """

    def __init__(self, config: OAuthConfig):
        """
        Initialize OAuth handler.

        Args:
            config: OAuth configuration
        """
        self.config = config
        self._http_client = httpx.AsyncClient(timeout=30.0)

        logger.info("OAuthHandler initialized", auth_url=config.authorization_url)

    def get_authorization_url(self, state: str, code_challenge: str = None) -> str:
        """
        Generate OAuth authorization URL.

        Args:
            state: State parameter for CSRF protection
            code_challenge: Optional PKCE code challenge

        Returns:
            Authorization URL
        """
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.config.scopes),
            "state": state,
        }

        if self.config.audience:
            params["audience"] = self.config.audience

        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"

        if self.config.extra_params:
            params.update(self.config.extra_params)

        return f"{self.config.authorization_url}?{urlencode(params)}"

    async def exchange_code(self, code: str, code_verifier: str = None) -> TokenInfo:
        """
        Exchange authorization code for tokens.

        Args:
            code: Authorization code
            code_verifier: Optional PKCE code verifier

        Returns:
            TokenInfo with access and refresh tokens
        """
        logger.info("Exchanging authorization code")

        data = {
            "grant_type": "authorization_code",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code": code,
            "redirect_uri": self.config.redirect_uri,
        }

        if code_verifier:
            data["code_verifier"] = code_verifier

        try:
            response = await self._http_client.post(
                self.config.token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()

            token_data = response.json()

            return self._parse_token_response(token_data)

        except httpx.HTTPError as e:
            logger.error("Token exchange failed", error=str(e))
            raise

    async def refresh_token(self, refresh_token: str) -> TokenInfo:
        """
        Refresh access token.

        Args:
            refresh_token: Refresh token

        Returns:
            New TokenInfo
        """
        logger.info("Refreshing access token")

        data = {
            "grant_type": "refresh_token",
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "refresh_token": refresh_token,
        }

        try:
            response = await self._http_client.post(
                self.config.token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()

            token_data = response.json()

            # Keep original refresh token if not returned
            token_info = self._parse_token_response(token_data)
            if not token_info.refresh_token:
                token_info.refresh_token = refresh_token

            return token_info

        except httpx.HTTPError as e:
            logger.error("Token refresh failed", error=str(e))
            raise

    def _parse_token_response(self, data: dict[str, Any]) -> TokenInfo:
        """Parse token response to TokenInfo."""
        expires_in = data.get("expires_in")
        expires_at = None

        if expires_in:
            expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        return TokenInfo(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=expires_at,
            token_type=data.get("token_type", "Bearer"),
            scopes=data.get("scope", "").split(" ") if data.get("scope") else [],
            metadata=data,
        )

    async def close(self):
        """Close HTTP client."""
        await self._http_client.aclose()


# ===== Provider-Specific OAuth Configs =====


def get_google_oauth_config(client_id: str, client_secret: str, redirect_uri: str, scopes: list[str]) -> OAuthConfig:
    """Get Google OAuth configuration."""
    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",  # noqa: S106 — OAuth token_url kwarg name, not a secret
        scopes=scopes,
        redirect_uri=redirect_uri,
        extra_params={"access_type": "offline", "prompt": "consent"},
    )


def get_microsoft_oauth_config(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: list[str],
    tenant: str = "common",
) -> OAuthConfig:
    """Get Microsoft OAuth configuration."""
    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        authorization_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        token_url=f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        scopes=scopes,
        redirect_uri=redirect_uri,
    )


def get_atlassian_oauth_config(client_id: str, client_secret: str, redirect_uri: str, scopes: list[str]) -> OAuthConfig:
    """Get Atlassian (Confluence/Jira) OAuth configuration."""
    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        authorization_url="https://auth.atlassian.com/authorize",
        token_url="https://auth.atlassian.com/oauth/token",  # noqa: S106 — OAuth token_url kwarg name, not a secret
        scopes=scopes,
        redirect_uri=redirect_uri,
        audience="api.atlassian.com",
        extra_params={"prompt": "consent"},
    )


def get_slack_oauth_config(client_id: str, client_secret: str, redirect_uri: str, scopes: list[str]) -> OAuthConfig:
    """Get Slack OAuth configuration."""
    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        authorization_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",  # noqa: S106 — OAuth token_url kwarg name, not a secret
        scopes=scopes,
        redirect_uri=redirect_uri,
    )


def get_salesforce_oauth_config(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: list[str],
    instance_url: str = "https://login.salesforce.com",
) -> OAuthConfig:
    """Get Salesforce OAuth configuration."""
    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        authorization_url=f"{instance_url}/services/oauth2/authorize",
        token_url=f"{instance_url}/services/oauth2/token",
        scopes=scopes,
        redirect_uri=redirect_uri,
    )
