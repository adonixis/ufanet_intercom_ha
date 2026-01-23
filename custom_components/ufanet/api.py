"""Minimal HTTP client for Ufanet intercom."""

from __future__ import annotations

import json
import logging
import time
import base64
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import async_timeout
from aiohttp import ClientResponseError, ClientSession

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://dom.ufanet.ru/"


class UfanetApiError(Exception):
    """Generic API error."""


class UfanetApiAuthError(UfanetApiError):
    """Authentication/authorization error."""


@dataclass
class IntercomInfo:
    """Intercom info subset used by the integration."""

    id: int
    role_name: Optional[str] = None
    string_view: Optional[str] = None
    custom_name: Optional[str] = None
    address: Optional[str] = None


@dataclass
class CameraInfo:
    """Camera info needed for streaming."""

    number: str
    title: Optional[str]
    address: Optional[str]
    domain: str
    token_l: str
    screenshot_domain: Optional[str] = None


class UfanetApiClient:
    """HTTP client for Ufanet intercom."""

    def __init__(
        self,
        session: ClientSession,
        contract: str,
        *,
        password: Optional[str] = None,
        refresh_token: Optional[str] = None,
        refresh_exp: Optional[int] = None,
    ) -> None:
        self._session = session
        self._contract = contract
        self._password = password
        self._access_token: Optional[str] = None
        self._access_exp: Optional[int] = None
        self._refresh_token: Optional[str] = refresh_token
        self._refresh_exp: Optional[int] = refresh_exp

    async def async_get_intercoms(self, on_token_update=None) -> List[IntercomInfo]:
        """Authenticate (if needed) and get intercom list."""
        await self._ensure_access_token(on_token_update)
        try:
            data = await self._request("GET", "api/v0/skud/shared/")
        except UfanetApiAuthError:
            await self._login(on_token_update)
            data = await self._request("GET", "api/v0/skud/shared/")

        intercoms: List[IntercomInfo] = []
        for item in data or []:
            intercoms.append(
                IntercomInfo(
                    id=item.get("id"),
                    role_name=item.get("role").get("name"),
                    string_view=item.get("string_view"),
                    custom_name=item.get("custom_name"),
                    address=item.get("address"),
                )
            )
        return intercoms

    async def async_open_intercom(self, intercom_id: int, on_token_update=None) -> bool:
        """Authenticate (if needed) and open an intercom."""
        await self._ensure_access_token(on_token_update)
        try:
            data = await self._request("GET", f"api/v0/skud/shared/{intercom_id}/open/")
        except UfanetApiAuthError:
            await self._login(on_token_update)
            data = await self._request("GET", f"api/v0/skud/shared/{intercom_id}/open/")
        return bool(data and data.get("result"))


    async def async_get_cameras(self, on_token_update=None) -> List[CameraInfo]:
        """Get list of cameras with prepared stream info from dom API."""
        await self._ensure_access_token(on_token_update)
        data = await self._request("GET", "api/v1/cctv")
        _LOGGER.debug("Raw camera response type=%s", type(data).__name__)
        cameras: List[CameraInfo] = []
        results = data if isinstance(data, list) else []
        _LOGGER.debug("Camera results count=%s", len(results))
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

    async def _login(self, on_token_update=None) -> None:
        """Full login to obtain access and refresh tokens (requires password)."""
        if not self._password:
            raise UfanetApiAuthError(
                "Refresh token expired. Please reconfigure the integration."
            )

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
            raise UfanetApiAuthError("No token in response")

        self._access_token = access
        self._access_exp = self._extract_exp(access)
        self._refresh_token = refresh
        self._refresh_exp = refresh_exp

        if on_token_update:
            await on_token_update(refresh, refresh_exp)

    async def _refresh_access_token(self, on_token_update=None) -> None:
        """Refresh access (and refresh) token using refresh token."""
        if not self._refresh_token:
            raise UfanetApiAuthError("No refresh token available")

        data = await self._request(
            "POST",
            "api/v1/auth/refresh/",
            json={"token": self._refresh_token},
            include_token=False,
        )

        if not isinstance(data, dict):
            raise UfanetApiAuthError("Invalid refresh response")

        access = data.get("access")
        refresh = data.get("refresh")
        refresh_exp = data.get("exp")
        if not (access and refresh):
            raise UfanetApiAuthError("Refresh failed")

        self._access_token = access
        self._access_exp = self._extract_exp(access)
        self._refresh_token = refresh
        self._refresh_exp = refresh_exp

        cb = on_token_update or getattr(self, "_token_update_cb", None)
        if cb:
            await cb(refresh, refresh_exp)

    async def _ensure_access_token(self, on_token_update=None) -> None:
        """Ensure a valid access token is available."""
        if on_token_update:
            self._token_update_cb = on_token_update
        if self._access_token and not self._is_expiring(self._access_exp):
            return

        # Try refresh token if present (even if exp unknown)
        if self._refresh_token and (self._refresh_exp is None or not self._is_expiring(self._refresh_exp)):
            try:
                await self._refresh_access_token(on_token_update)
                return
            except UfanetApiAuthError:
                # Refresh token expired; will try password below if available
                pass

        # If we have a password (initial login or re-login), attempt full login
        if self._password:
            await self._login(on_token_update)
            return

        # No refresh token and no password -> require reconfiguration
        raise UfanetApiAuthError(
            "No valid token available. Please reconfigure the integration."
        )

    @staticmethod
    def _extract_exp(token: Optional[str]) -> Optional[int]:
        """Extract exp from JWT without verification."""
        if not token:
            return None
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode()))
            return int(payload.get("exp")) if "exp" in payload else None
        except Exception:
            return None

    @staticmethod
    def _is_expiring(exp: Optional[int], skew_seconds: int = 60) -> bool:
        """Return True if token is close to expiration (or we don't know exp)."""
        if exp is None:
            return True
        now = int(time.time())
        return now >= (exp - skew_seconds)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        include_token: bool = True,
        timeout: int = 30,
        base_url: str = BASE_URL,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Perform HTTP request with optional JWT token."""
        headers: Dict[str, str] = {}
        if include_token and self._access_token:
            headers["Authorization"] = f"JWT {self._access_token}"
        if extra_headers:
            headers.update(extra_headers)

        url = urljoin(base_url, path)

        try:
            async with async_timeout.timeout(timeout):
                async with self._session.request(
                    method, url, json=json, params=params, headers=headers
                ) as resp:
                    text = await resp.text()
                    if resp.status == 401 and include_token:
                        # Attempt refresh once, then retry
                        await self._refresh_access_token()
                        headers["Authorization"] = f"JWT {self._access_token}"
                        async with self._session.request(
                            method, url, json=json, params=params, headers=headers
                        ) as retry_resp:
                            retry_text = await retry_resp.text()
                            if retry_resp.status >= 400:
                                raise UfanetApiError(f"{retry_resp.status}: {retry_text}")
                            try:
                                return await retry_resp.json(content_type=None)
                            except Exception:
                                return retry_text
                    if resp.status >= 400:
                        raise UfanetApiError(f"{resp.status}: {text}")
                    try:
                        return await resp.json(content_type=None)
                    except Exception:
                        return text
        except ClientResponseError as err:
            if err.status == 401:
                raise UfanetApiAuthError(str(err)) from err
            raise UfanetApiError(str(err)) from err


