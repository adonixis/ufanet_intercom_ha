"""Minimal HTTP client for Ufanet intercom."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import async_timeout
from aiohttp import ClientResponseError, ClientSession

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

BASE_URL = "https://dom.ufanet.ru/"

# HTTP status codes
HTTP_STATUS_OK = 200
HTTP_STATUS_BAD_REQUEST = 400
HTTP_STATUS_UNAUTHORIZED = 401

# JWT token parts count
JWT_PARTS_COUNT = 3

# Token expiration skew in seconds
TOKEN_EXPIRATION_SKEW = 60


class UfanetApiError(Exception):
    """Generic API error."""


class UfanetApiAuthError(UfanetApiError):
    """Authentication/authorization error."""


@dataclass
class IntercomInfo:
    """Intercom info subset used by the integration."""

    id: int
    role_name: str | None = None
    string_view: str | None = None
    custom_name: str | None = None
    address: str | None = None


def intercom_display_name(intercom: IntercomInfo) -> str:
    """Return a display name for an intercom entity."""
    return (
        intercom.role_name
        or intercom.string_view
        or intercom.custom_name
        or f"Intercom {intercom.id}"
    )


@dataclass
class CameraInfo:
    """Camera info needed for streaming."""

    number: str
    title: str | None
    address: str | None
    domain: str
    token_l: str
    screenshot_domain: str | None = None


class UfanetApiClient:
    """HTTP client for Ufanet intercom."""

    def __init__(
        self,
        session: ClientSession,
        contract: str,
        *,
        password: str | None = None,
        refresh_token: str | None = None,
        refresh_exp: int | None = None,
    ) -> None:
        """
        Initialize the API client.

        Args:
            session: aiohttp client session
            contract: Contract number
            password: Optional password for authentication
            refresh_token: Optional refresh token
            refresh_exp: Optional refresh token expiration timestamp

        """
        self._session = session
        self._contract = contract
        self._password = password
        self._access_token: str | None = None
        self._access_exp: int | None = None
        self._refresh_token: str | None = refresh_token
        self._refresh_exp: int | None = refresh_exp

    async def async_get_intercoms(
        self, on_token_update: Callable[[str, int], None] | None = None
    ) -> list[IntercomInfo]:
        """Authenticate (if needed) and get intercom list."""
        await self._ensure_access_token(on_token_update)
        try:
            data = await self._request("GET", "api/v0/skud/shared/")
        except UfanetApiAuthError:
            await self._login(on_token_update)
            data = await self._request("GET", "api/v0/skud/shared/")

        return [
            IntercomInfo(
                id=item.get("id"),
                role_name=(
                    item.get("role", {}).get("name")
                    if item.get("role")
                    else None
                ),
                string_view=item.get("string_view"),
                custom_name=item.get("custom_name"),
                address=item.get("address"),
            )
            for item in data or []
        ]

    async def async_open_intercom(
        self,
        intercom_id: int,
        on_token_update: Callable[[str, int], None] | None = None,
    ) -> bool:
        """Authenticate (if needed) and open an intercom."""
        await self._ensure_access_token(on_token_update)
        try:
            data = await self._request("GET", f"api/v0/skud/shared/{intercom_id}/open/")
        except UfanetApiAuthError:
            await self._login(on_token_update)
            data = await self._request("GET", f"api/v0/skud/shared/{intercom_id}/open/")
        return bool(data and data.get("result"))

    async def async_get_cameras(
        self, on_token_update: Callable[[str, int], None] | None = None
    ) -> list[CameraInfo]:
        """Get list of cameras with prepared stream info from dom API."""
        await self._ensure_access_token(on_token_update)
        data = await self._request("GET", "api/v1/cctv")
        cameras: list[CameraInfo] = []
        results = data if isinstance(data, list) else []
        for item in results or []:
            servers = item.get("servers", {})
            domain = servers.get("domain")
            screenshot_domain = servers.get("screenshot_domain")
            number = item.get("number")
            token_l = item.get("token_l")
            if not (domain and number and token_l):
                continue
            cameras.append(
                CameraInfo(
                    number=number,
                    title=item.get("title"),
                    address=item.get("address"),
                    domain=domain,
                    token_l=token_l,
                    screenshot_domain=screenshot_domain,
                )
            )
        return cameras

    async def _login(
        self, on_token_update: Callable[[str, int], None] | None = None
    ) -> None:
        """Full login to obtain access and refresh tokens (requires password)."""
        if not self._password:
            msg = "Refresh token expired. Please reconfigure the integration."
            raise UfanetApiAuthError(msg)

        data = await self._request(
            "POST",
            "api/v1/auth/auth_by_contract/",
            json={"contract": self._contract, "password": self._password},
            include_token=False,
        )
        token_info = (data or {}).get("token", {}) if isinstance(data, dict) else {}
        access = token_info.get("access")
        refresh = token_info.get("refresh")
        refresh_exp = token_info.get("exp")
        if not (access and refresh):
            msg = "No token in response"
            raise UfanetApiAuthError(msg)

        self._access_token = access
        self._access_exp = self._extract_exp(access)
        self._refresh_token = refresh
        self._refresh_exp = refresh_exp

        if on_token_update:
            await on_token_update(refresh, refresh_exp)

    async def _refresh_access_token(
        self, on_token_update: Callable[[str, int], None] | None = None
    ) -> None:
        """Refresh access (and refresh) token using refresh token."""
        if not self._refresh_token:
            msg = "No refresh token available"
            raise UfanetApiAuthError(msg)

        data = await self._request(
            "POST",
            "api/v1/auth/refresh/",
            json={"token": self._refresh_token},
            include_token=False,
        )

        if not isinstance(data, dict):
            msg = "Invalid refresh response"
            raise UfanetApiAuthError(msg)

        access = data.get("access")
        refresh = data.get("refresh")
        refresh_exp = data.get("exp")
        if not (access and refresh):
            msg = "Refresh failed"
            raise UfanetApiAuthError(msg)

        self._access_token = access
        self._access_exp = self._extract_exp(access)
        self._refresh_token = refresh
        self._refresh_exp = refresh_exp

        cb = on_token_update or getattr(self, "_token_update_cb", None)
        if cb:
            await cb(refresh, refresh_exp)

    async def _ensure_access_token(
        self, on_token_update: Callable[[str, int], None] | None = None
    ) -> None:
        """Ensure a valid access token is available."""
        if on_token_update:
            self._token_update_cb = on_token_update
        if self._access_token and not self._is_expiring(self._access_exp):
            return

        # Try refresh token if present (even if exp unknown)
        refresh_exp_ok = self._refresh_exp is None or not self._is_expiring(
            self._refresh_exp
        )
        if self._refresh_token and refresh_exp_ok:
            try:
                await self._refresh_access_token(on_token_update)
            except UfanetApiAuthError:
                # Refresh token expired; will try password below if available
                pass
            else:
                return

        # If we have a password (initial login or re-login), attempt full login
        if self._password:
            await self._login(on_token_update)
            return

        # No refresh token and no password -> require reconfiguration
        msg = "No valid token available. Please reconfigure the integration."
        raise UfanetApiAuthError(msg)

    @staticmethod
    def _extract_exp(token: str | None) -> int | None:
        """Extract exp from JWT without verification."""
        if not token:
            return None
        try:
            parts = token.split(".")
            if len(parts) != JWT_PARTS_COUNT:
                return None
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
            return int(payload.get("exp")) if "exp" in payload else None
        except (ValueError, KeyError, json.JSONDecodeError):
            return None

    @staticmethod
    def _is_expiring(
        exp: int | None, skew_seconds: int = TOKEN_EXPIRATION_SKEW
    ) -> bool:
        """Return True if token is close to expiration (or we don't know exp)."""
        if exp is None:
            return True
        now = int(time.time())
        return now >= (exp - skew_seconds)

    @staticmethod
    def extract_exp(token: str | None) -> int | None:
        """Public wrapper for extracting exp from a JWT."""
        return UfanetApiClient._extract_exp(token)

    @staticmethod
    def is_expiring(exp: int | None, skew_seconds: int = TOKEN_EXPIRATION_SKEW) -> bool:
        """Public wrapper for checking whether a token is expiring."""
        return UfanetApiClient._is_expiring(exp, skew_seconds=skew_seconds)

    async def _request(  # noqa: PLR0913
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        include_token: bool = True,
        timeout_seconds: int = 30,
        base_url: str = BASE_URL,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """Perform HTTP request with optional JWT token."""
        headers: dict[str, str] = {}
        if include_token and self._access_token:
            headers["Authorization"] = f"JWT {self._access_token}"
        if extra_headers:
            headers.update(extra_headers)

        url = urljoin(base_url, path)

        try:
            async with async_timeout.timeout(timeout_seconds):  # noqa: SIM117
                async with self._session.request(
                    method, url, json=json, params=params, headers=headers
                ) as resp:
                    text = await resp.text()
                    if resp.status == HTTP_STATUS_UNAUTHORIZED and include_token:
                        # Attempt refresh once, then retry
                        await self._refresh_access_token()
                        headers["Authorization"] = f"JWT {self._access_token}"
                        async with self._session.request(
                            method, url, json=json, params=params, headers=headers
                        ) as retry_resp:
                            retry_text = await retry_resp.text()
                            if retry_resp.status >= HTTP_STATUS_BAD_REQUEST:
                                error_msg = f"{retry_resp.status}: {retry_text}"
                                raise UfanetApiError(error_msg)
                            try:
                                return await retry_resp.json(content_type=None)
                            except (ValueError, KeyError):
                                return retry_text
                    if resp.status >= HTTP_STATUS_BAD_REQUEST:
                        error_msg = f"{resp.status}: {text}"
                        raise UfanetApiError(error_msg)
                    try:
                        return await resp.json(content_type=None)
                    except (ValueError, KeyError):
                        return text
        except ClientResponseError as err:
            if err.status == HTTP_STATUS_UNAUTHORIZED:
                raise UfanetApiAuthError(str(err)) from err
            raise UfanetApiError(str(err)) from err
