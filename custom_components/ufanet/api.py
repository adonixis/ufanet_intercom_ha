"""
Minimal HTTP client for Ufanet intercom.

Auth flow (Dom API):
- login via POST /api/v1/auth/auth_by_contract/
- renew via POST /api/v1/auth/refresh/ (refresh JWT lives ~months)
- refresh/access expiry taken from JWT payload, not from the login
  response's nested ``token.exp`` (that field is the *access* TTL ~24h)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientResponseError, ClientSession

from .const import (
    DEFAULT_DOM_URL,
    PHONE_APPLICATION_ID,
    PHONE_COUNTRY_ID,
)

_LOGGER = logging.getLogger(__name__)

BASE_URL = DEFAULT_DOM_URL

HTTP_STATUS_BAD_REQUEST = 400
HTTP_STATUS_UNAUTHORIZED = 401

JWT_PARTS_COUNT = 3

# Renew a few minutes before wall-clock expiry.
TOKEN_REFRESH_BUFFER = 300

TokenUpdateCallback = Callable[[str, int | None], Awaitable[None]]


class UfanetApiError(Exception):
    """Generic API error."""


class UfanetApiAuthError(UfanetApiError):
    """Authentication/authorization error."""


class PhoneAuthError(UfanetApiError):
    """Raised when a phone-call auth step fails."""


async def _phone_post(
    session: ClientSession,
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = urljoin(base_url if base_url.endswith("/") else base_url + "/", path)
    async with session.post(url, json=payload) as resp:
        resp.raise_for_status()
        return await resp.json()


async def phone_auth_init(
    session: ClientSession, base_url: str, phone: str
) -> dict[str, Any]:
    """Start a call-auth attempt. Returns {phone_to_call, request_id, timeout}."""
    data = await _phone_post(
        session,
        base_url,
        "api/v4/phone_auth/call/init/",
        {
            "phone": phone,
            "application_id": PHONE_APPLICATION_ID,
            "country_id": PHONE_COUNTRY_ID,
        },
    )
    payload = data.get("data") or {}
    if not payload.get("request_id") or not payload.get("phone_to_call"):
        raise PhoneAuthError(f"Unexpected init response: {data}")
    return payload


async def phone_auth_contact_list(
    session: ClientSession, base_url: str, request_id: str
) -> list[dict[str, Any]]:
    """Return contracts tied to the calling number once the call landed."""
    data = await _phone_post(
        session,
        base_url,
        "api/v4/phone_auth/call/contact_list/",
        {"request_id": request_id},
    )
    return ((data.get("data") or {}).get("contracts")) or []


async def phone_auth_apply(
    session: ClientSession, base_url: str, request_id: str, contract_id: int
) -> dict[str, Any]:
    """Exchange a confirmed call for the dom JWT pair {access, refresh}."""
    data = await _phone_post(
        session,
        base_url,
        "api/v4/phone_auth/call/apply/",
        {"request_id": request_id, "contract_id": contract_id},
    )
    payload = data.get("data") or {}
    if not payload.get("access") or not payload.get("refresh"):
        raise PhoneAuthError(f"Unexpected apply response: {data}")
    return payload


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
        access_token: str | None = None,
        refresh_token: str | None = None,
        refresh_exp: int | None = None,
        base_url: str = BASE_URL,
        allow_password_login: bool = True,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self._contract = contract
        self._password = password
        self._allow_password_login = allow_password_login and bool(password)
        self._access_token: str | None = access_token
        self._access_exp: int | None = (
            self._extract_exp(access_token) if access_token else None
        )
        self._refresh_token: str | None = refresh_token
        # Prefer JWT-decoded exp; fall back to a stored value, then decode now.
        self._refresh_exp: int | None = refresh_exp
        if self._refresh_token and not self._refresh_exp:
            self._refresh_exp = self._extract_exp(self._refresh_token)
        self._token_update_cb: TokenUpdateCallback | None = None
        self._base_url = base_url or BASE_URL
        self._token_lock = asyncio.Lock()

    def set_token_update_callback(self, callback: TokenUpdateCallback | None) -> None:
        """Register a callback invoked when the refresh token rotates."""
        self._token_update_cb = callback

    def _store_tokens(self, access: str, refresh: str | None) -> None:
        """
        Apply a new access/refresh pair.

        Expiry is always taken from the JWT payload.
        The login response nests tokens under ``token.*`` and its ``exp`` is the
        *access* TTL (~24h), not the refresh TTL (~months) — trusting it caused
        a full password login every day.
        """
        self._access_token = access
        self._access_exp = self._extract_exp(access)
        if refresh:
            self._refresh_token = refresh
            self._refresh_exp = self._extract_exp(refresh)

    async def _persist_refresh(
        self, on_token_update: TokenUpdateCallback | None = None
    ) -> None:
        cb = on_token_update or self._token_update_cb
        if cb and self._refresh_token:
            await cb(self._refresh_token, self._refresh_exp)

    async def async_get_intercoms(self) -> list[IntercomInfo]:
        """Authenticate (if needed) and get intercom list."""
        await self._ensure_access_token()
        try:
            data = await self._request("GET", "api/v0/skud/shared/")
        except UfanetApiAuthError:
            await self._login()
            data = await self._request("GET", "api/v0/skud/shared/")

        return [
            IntercomInfo(
                id=item.get("id"),
                role_name=(
                    item.get("role", {}).get("name") if item.get("role") else None
                ),
                string_view=item.get("string_view"),
                custom_name=item.get("custom_name"),
                address=item.get("address"),
            )
            for item in data or []
        ]

    async def async_open_intercom(self, intercom_id: int) -> bool:
        """Authenticate (if needed) and open an intercom."""
        await self._ensure_access_token()
        try:
            data = await self._request("GET", f"api/v0/skud/shared/{intercom_id}/open/")
        except UfanetApiAuthError:
            await self._login()
            data = await self._request("GET", f"api/v0/skud/shared/{intercom_id}/open/")
        return bool(data and data.get("result"))

    async def async_get_cameras(self) -> list[CameraInfo]:
        """Get list of cameras with prepared stream info from dom API."""
        await self._ensure_access_token()
        try:
            data = await self._request("GET", "api/v1/cctv")
        except UfanetApiAuthError:
            await self._login()
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

    async def _login(self) -> None:
        """Full login to obtain access and refresh tokens (requires password)."""
        if not self._allow_password_login or not self._password:
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
        if not (access and refresh):
            msg = "No token in response"
            raise UfanetApiAuthError(msg)

        self._store_tokens(access, refresh)
        await self._persist_refresh()

    async def _refresh_access_token(self) -> bool:
        """
        Try to renew access via the refresh token. Returns True on success.

        Refresh access token using the stored refresh JWT:
        POST /api/v1/auth/refresh/ with ``{"token": "<refresh-jwt>"}`` returns a
        flat ``{"access", "refresh", "exp"}`` (not nested under ``token``).
        Soft-fails (False) so the caller can fall back to password login.
        """
        if not self._refresh_token:
            return False

        now = int(time.time())
        if self._refresh_exp and now >= self._refresh_exp:
            _LOGGER.debug("Refresh JWT expired; will full-login")
            return False

        try:
            data = await self._request(
                "POST",
                "api/v1/auth/refresh/",
                json={"token": self._refresh_token},
                include_token=False,
            )
        except UfanetApiError as err:
            _LOGGER.debug("Refresh rejected (%s); will full-login", err)
            return False

        if not isinstance(data, dict):
            return False

        access = data.get("access")
        if not access:
            return False

        self._store_tokens(access, data.get("refresh"))
        await self._persist_refresh()
        return True

    async def _ensure_access_token(self) -> None:
        """Ensure a valid access token is available (refresh → login)."""
        async with self._token_lock:
            now = int(time.time())
            access_fresh = (
                self._access_token
                and self._access_exp is not None
                and now < self._access_exp - TOKEN_REFRESH_BUFFER
            )
            if access_fresh:
                return

            if await self._refresh_access_token():
                return

            if self._allow_password_login:
                await self._login()
                return

            msg = "No valid token available. Please reconfigure the integration."
            raise UfanetApiAuthError(msg)

    @staticmethod
    def _extract_exp(token: str | None) -> int | None:
        """Extract exp from JWT payload without verification."""
        if not token:
            return None
        try:
            parts = token.split(".")
            if len(parts) != JWT_PARTS_COUNT:
                return None
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
            return int(payload.get("exp")) if "exp" in payload else None
        except (ValueError, KeyError, json.JSONDecodeError, IndexError):
            return None

    @staticmethod
    def _is_expiring(exp: int | None, skew_seconds: int = TOKEN_REFRESH_BUFFER) -> bool:
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
    def is_expiring(exp: int | None, skew_seconds: int = TOKEN_REFRESH_BUFFER) -> bool:
        """Public wrapper for checking whether a token is expiring."""
        return UfanetApiClient._is_expiring(exp, skew_seconds=skew_seconds)

    async def _request(  # noqa: PLR0912
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        include_token: bool = True,
        timeout_seconds: int = 30,
        base_url: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """Perform HTTP request with optional JWT token."""
        headers: dict[str, str] = {}
        if include_token and self._access_token:
            headers["Authorization"] = f"JWT {self._access_token}"
        if extra_headers:
            headers.update(extra_headers)

        url = urljoin(base_url or self._base_url, path)

        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._session.request(
                    method, url, json=json, params=params, headers=headers
                ) as resp:
                    text = await resp.text()
                    if resp.status == HTTP_STATUS_UNAUTHORIZED and include_token:
                        # Soft refresh, then password login if needed.
                        renewed = await self._refresh_access_token()
                        if not renewed:
                            if self._allow_password_login:
                                await self._login()
                            else:
                                raise UfanetApiAuthError(f"{resp.status}: {text}")
                        headers["Authorization"] = f"JWT {self._access_token}"
                        async with self._session.request(
                            method, url, json=json, params=params, headers=headers
                        ) as retry_resp:
                            retry_text = await retry_resp.text()
                            if retry_resp.status == HTTP_STATUS_UNAUTHORIZED:
                                raise UfanetApiAuthError(
                                    f"{retry_resp.status}: {retry_text}"
                                )
                            if retry_resp.status >= HTTP_STATUS_BAD_REQUEST:
                                error_msg = f"{retry_resp.status}: {retry_text}"
                                raise UfanetApiError(error_msg)
                            try:
                                return await retry_resp.json(content_type=None)
                            except (ValueError, KeyError):
                                return retry_text
                    if resp.status == HTTP_STATUS_UNAUTHORIZED:
                        raise UfanetApiAuthError(f"{resp.status}: {text}")
                    if resp.status >= HTTP_STATUS_BAD_REQUEST:
                        error_msg = f"{resp.status}: {text}"
                        raise UfanetApiError(error_msg)
                    try:
                        return await resp.json(content_type=None)
                    except (ValueError, KeyError):
                        return text
        except TimeoutError as err:
            raise UfanetApiError("Request timed out") from err
        except ClientResponseError as err:
            if err.status == HTTP_STATUS_UNAUTHORIZED:
                raise UfanetApiAuthError(str(err)) from err
            raise UfanetApiError(str(err)) from err
