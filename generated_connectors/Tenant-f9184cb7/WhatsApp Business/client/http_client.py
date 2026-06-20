from __future__ import annotations

from typing import Any

import aiohttp

from exceptions import (
    WhatsAppAuthError,
    WhatsAppError,
    WhatsAppNetworkError,
    WhatsAppNotFoundError,
    WhatsAppRateLimitError,
)

WHATSAPP_BASE_URL = "https://graph.facebook.com/v18.0"
DEFAULT_TIMEOUT_S = 30.0


class WhatsAppHTTPClient:
    """Low-level async HTTP client for the Meta WhatsApp Business Cloud API."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                base_url=WHATSAPP_BASE_URL,
                timeout=self._timeout,
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        access_token: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        session = self._get_session()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        try:
            async with session.request(
                method,
                path,
                headers=headers,
                **kwargs,
            ) as response:
                body: dict[str, Any] = {}
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    pass

                if response.status == 200:
                    return body

                # Parse Meta Graph API error envelope
                error: dict[str, Any] = body.get("error", {})
                err_code: int = int(error.get("code", 0))
                err_subcode: int = int(error.get("error_subcode", 0))
                err_msg: str = error.get("message", f"HTTP {response.status}")

                if response.status == 429:
                    retry_after = float(response.headers.get("Retry-After", "0"))
                    raise WhatsAppRateLimitError(
                        f"Rate limited: {err_msg}", retry_after=retry_after
                    )

                if response.status >= 500:
                    raise WhatsAppNetworkError(
                        f"Meta server error {response.status}: {err_msg}",
                        status_code=response.status,
                        code=err_code,
                    )

                # Meta error code 190 = invalid/expired token
                if err_code == 190:
                    raise WhatsAppAuthError(
                        f"Invalid or expired access token: {err_msg}",
                        status_code=response.status,
                        code=err_code,
                    )

                # Meta error code 100 + subcode 33 = not found
                if err_code == 100 and err_subcode == 33:
                    raise WhatsAppNotFoundError("resource", path)

                # Generic Meta parameter / validation error
                if err_code == 100:
                    raise WhatsAppError(
                        f"Invalid parameter: {err_msg}",
                        status_code=response.status,
                        code=err_code,
                    )

                raise WhatsAppError(
                    f"Meta Graph API error {response.status} (code={err_code}): {err_msg}",
                    status_code=response.status,
                    code=err_code,
                )

        except (
            WhatsAppError,
            WhatsAppAuthError,
            WhatsAppNetworkError,
            WhatsAppRateLimitError,
            WhatsAppNotFoundError,
        ):
            raise
        except aiohttp.ServerTimeoutError as exc:
            raise WhatsAppNetworkError(f"Request timed out: {exc}") from exc
        except aiohttp.ClientConnectionError as exc:
            raise WhatsAppNetworkError(f"Connection error: {exc}") from exc
        except Exception as exc:
            raise WhatsAppNetworkError(f"Unexpected network error: {exc}") from exc

    # ── Phone number ──────────────────────────────────────────────────────────

    async def get_phone_number(
        self,
        access_token: str,
        phone_number_id: str,
    ) -> dict[str, Any]:
        """Verify credentials and fetch phone number metadata."""
        return await self._request(
            "GET",
            f"/{phone_number_id}",
            access_token,
            params={
                "fields": "display_phone_number,verified_name,quality_rating,status"
            },
        )

    # ── Message templates ─────────────────────────────────────────────────────

    async def list_templates(
        self,
        access_token: str,
        waba_id: str,
        limit: int = 20,
        after: str | None = None,
    ) -> dict[str, Any]:
        """List message templates for a WABA with cursor pagination."""
        params: dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        return await self._request(
            "GET",
            f"/{waba_id}/message_templates",
            access_token,
            params=params,
        )

    async def get_template(
        self,
        access_token: str,
        template_id: str,
    ) -> dict[str, Any]:
        """Fetch a single message template by ID."""
        return await self._request(
            "GET",
            f"/{template_id}",
            access_token,
            params={
                "fields": "name,status,category,language,components"
            },
        )

    # ── Phone numbers in WABA ─────────────────────────────────────────────────

    async def list_phone_numbers(
        self,
        access_token: str,
        waba_id: str,
    ) -> dict[str, Any]:
        """List all phone numbers registered to the WABA."""
        return await self._request(
            "GET",
            f"/{waba_id}/phone_numbers",
            access_token,
            params={
                "fields": "display_phone_number,verified_name,quality_rating,status"
            },
        )

    # ── WABA details ──────────────────────────────────────────────────────────

    async def get_waba(
        self,
        access_token: str,
        waba_id: str,
    ) -> dict[str, Any]:
        """Fetch WhatsApp Business Account details."""
        return await self._request(
            "GET",
            f"/{waba_id}",
            access_token,
            params={
                "fields": "name,currency,timezone_id,message_template_namespace"
            },
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> WhatsAppHTTPClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()
