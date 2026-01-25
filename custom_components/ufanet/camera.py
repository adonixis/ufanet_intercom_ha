"""Camera platform for Ufanet Intercom."""

from __future__ import annotations

from typing import TYPE_CHECKING

import async_timeout
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo

from .api import CameraInfo, UfanetApiAuthError, UfanetApiClient, UfanetApiError
from .const import CONF_CONTRACT, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up cameras."""
    data = hass.data[DOMAIN][entry.entry_id]
    session = async_get_clientsession(hass)

    # Create callback to save token updates
    store = data.get("_store")
    contract = data.get("_contract")

    async def save_token(token: str, exp: int) -> None:
        if store and contract:
            stored_data = await store.async_load() or {}
            if contract not in stored_data:
                stored_data[contract] = {}
            stored_data[contract]["refresh_token"] = token
            stored_data[contract]["refresh_exp"] = exp
            await store.async_save(stored_data)

    # Try to get password from secure storage for re-authentication if needed
    password = None
    if store and contract:
        stored_data = await store.async_load() or {}
        credentials = stored_data.get(contract, {})
        password = credentials.get("password")

    client = UfanetApiClient(
        session,
        data[CONF_CONTRACT],
        password=password,
        refresh_token=data.get("refresh_token"),
        refresh_exp=data.get("refresh_exp"),
    )

    try:
        cameras = await client.async_get_cameras(on_token_update=save_token)
    except UfanetApiAuthError:
        cameras = []
    except UfanetApiError:
        cameras = []
    except Exception:  # noqa: BLE001
        cameras = []

    if not cameras:
        return

    # Create camera entity for each camera in the list, sharing a single API client
    entities = [UfanetCamera(entry, cam, hass, client) for cam in cameras]
    async_add_entities(entities, update_before_add=True)


class UfanetCamera(Camera):
    """Camera entity for Ufanet streams."""

    def __init__(
        self,
        entry: ConfigEntry,
        cam: CameraInfo,
        hass: HomeAssistant,
        client: UfanetApiClient,
    ) -> None:
        """Initialize the camera entity."""
        super().__init__()
        self._entry = entry
        self._cam = cam
        self._hass = hass
        self._client = client
        self._token_exp: int | None = UfanetApiClient.extract_exp(cam.token_l)
        self._attr_unique_id = f"{entry.entry_id}_{cam.number}"
        self._attr_name = cam.title or cam.address or cam.number
        # Initialize URLs
        self._stream_url = ""
        self._screenshot_url: str | None = None
        self._update_urls()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data.get(CONF_CONTRACT))},
            name=entry.data.get(CONF_CONTRACT),
            manufacturer="Ufanet",
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
        # If we know exp and it is not expiring soon, do nothing
        if self._token_exp is not None and not UfanetApiClient.is_expiring(
            self._token_exp
        ):
            return

        try:
            cameras = await self._client.async_get_cameras()
        except UfanetApiAuthError:
            # If refresh fails, keep using existing URLs
            return
        except Exception:  # noqa: BLE001
            # If refresh fails, keep using existing URLs
            return

        for cam in cameras:
            if cam.number == self._cam.number:
                self._cam = cam
                self._token_exp = UfanetApiClient.extract_exp(cam.token_l)
                self._update_urls()
                break

    @property
    def unique_id(self) -> str:
        """Return a unique ID."""
        return self._attr_unique_id

    @property
    def name(self) -> str:
        """Return the name of this camera."""
        return self._attr_name

    @property
    def supported_features(self) -> CameraEntityFeature:
        """Return supported features."""
        return CameraEntityFeature.STREAM

    @property
    def supports_stream(self) -> bool:
        """Advertise stream support explicitly."""
        return True

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
        session = async_get_clientsession(self._hass)
        try:
            async with async_timeout.timeout(10):  # noqa: SIM117
                async with session.get(self._screenshot_url) as resp:
                    if resp.status == 200:  # noqa: PLR2004
                        return await resp.read()
                    return None
        except Exception:  # noqa: BLE001
            return None
