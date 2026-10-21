"""Camera platform for Ufanet Intercom."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo

from .api import UfanetApiAuthError, UfanetApiError
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import UfanetConfigEntry
    from .api import CameraInfo, UfanetApiClient

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UfanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up cameras."""
    client = entry.runtime_data.client

    try:
        cameras = await client.async_get_cameras()
    except UfanetApiAuthError:
        entry.async_start_reauth(hass)
        return
    except UfanetApiError:
        _LOGGER.exception("Failed to load cameras")
        return

    if not cameras:
        _LOGGER.debug("No cameras available for this account")
        return

    async_add_entities([UfanetCamera(entry, cam, client) for cam in cameras])


class UfanetCamera(Camera):
    """Camera entity for Ufanet streams."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: UfanetConfigEntry,
        cam: CameraInfo,
        client: UfanetApiClient,
    ) -> None:
        """Initialize the camera entity."""
        super().__init__()
        self._entry = entry
        self._cam = cam
        self._client = client
        self._token_exp: int | None = client.extract_exp(cam.token_l)
        self._contract = entry.runtime_data.contract
        self._attr_unique_id = f"{self._contract}_{cam.number}"
        self._attr_suggested_object_id = f"{self._contract}_{cam.number}"
        self._attr_name = cam.title or cam.address or cam.number
        self._stream_url = ""
        self._screenshot_url: str | None = None
        self._update_urls()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._contract}_cam_{cam.number}")},
            name=self._attr_name,
            manufacturer="Ufanet",
            model="Intercom camera",
            via_device=(DOMAIN, self._contract),
        )

    def _update_urls(self) -> None:
        """Update stream and screenshot URLs based on current camera info."""
        self._stream_url = (
            f"rtsp://{self._cam.domain}/{self._cam.number}?token={self._cam.token_l}"
        )
        if self._cam.screenshot_domain:
            self._screenshot_url = (
                f"https://{self._cam.screenshot_domain}/api/v0/screenshots/"
                f"{self._cam.number}~600.jpg?token={self._cam.token_l}"
            )
        else:
            self._screenshot_url = None

    async def _refresh_camera_token_if_needed(self) -> None:
        """Refresh camera token_l if it is close to expiration."""
        if self._token_exp is not None and not self._client.is_expiring(
            self._token_exp
        ):
            return

        try:
            cameras = await self._client.async_get_cameras()
        except UfanetApiAuthError:
            _LOGGER.warning(
                "Auth failed while refreshing camera token for %s",
                self._cam.number,
            )
            self._entry.async_start_reauth(self.hass)
            return
        except UfanetApiError as err:
            _LOGGER.warning(
                "Failed to refresh camera token for %s: %s",
                self._cam.number,
                err,
            )
            return

        for cam in cameras:
            if cam.number == self._cam.number:
                self._cam = cam
                self._token_exp = self._client.extract_exp(cam.token_l)
                self._update_urls()
                return

        _LOGGER.warning(
            "Camera %s missing from API response during token refresh",
            self._cam.number,
        )

    @property
    def supported_features(self) -> CameraEntityFeature:
        """Return supported features."""
        return CameraEntityFeature.STREAM

    async def stream_source(self) -> str | None:
        """Return the stream source."""
        await self._refresh_camera_token_if_needed()
        return self._stream_url

    async def async_camera_image(
        self,
        width: int | None = None,  # noqa: ARG002
        height: int | None = None,  # noqa: ARG002
    ) -> bytes | None:
        """Return a still image from the camera."""
        await self._refresh_camera_token_if_needed()

        if not self._screenshot_url:
            return None
        session = async_get_clientsession(self.hass)
        try:
            async with asyncio.timeout(10):
                async with session.get(self._screenshot_url) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    _LOGGER.debug(
                        "Screenshot HTTP %s for camera %s",
                        resp.status,
                        self._cam.number,
                    )
                    return None
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Screenshot fetch failed for camera %s",
                self._cam.number,
                exc_info=True,
            )
            return None
