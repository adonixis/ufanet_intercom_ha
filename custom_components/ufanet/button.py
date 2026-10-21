"""Button platform for Ufanet Intercom to open the selected intercom."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo

from .api import (
    UfanetApiAuthError,
    UfanetApiError,
    intercom_display_name,
)
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import UfanetConfigEntry


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: UfanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities for all intercoms."""
    intercoms = entry.runtime_data.intercoms

    async_add_entities(
        [
            UfanetOpenDoorButton(
                entry,
                intercom_id=intercom.id,
                intercom_name=intercom_display_name(intercom),
            )
            for intercom in intercoms
        ]
    )


class UfanetOpenDoorButton(ButtonEntity):
    """Button to open a specific intercom."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: UfanetConfigEntry,
        *,
        intercom_id: int,
        intercom_name: str,
    ) -> None:
        """Initialize the button entity."""
        self._entry = entry
        self._contract = entry.runtime_data.contract
        self._intercom_id = intercom_id

        self.entity_description = ButtonEntityDescription(
            key=f"open_intercom_{intercom_id}",
            translation_key="open_intercom",
            icon="mdi:door",
        )

        self._attr_unique_id = f"{self._contract}_{intercom_id}"
        self._attr_suggested_object_id = f"{self._contract}_{intercom_id}"
        self._attr_name = intercom_name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._contract}_{intercom_id}")},
            name=intercom_name,
            manufacturer="Ufanet",
            model="Intercom",
            via_device=(DOMAIN, self._contract),
        )

    async def async_press(self) -> None:
        """Handle the button press to open the intercom."""
        client = self._entry.runtime_data.client
        try:
            opened = await client.async_open_intercom(self._intercom_id)
        except UfanetApiAuthError as err:
            self._entry.async_start_reauth(self.hass)
            msg = "Authentication failed; please reauthenticate"
            raise HomeAssistantError(msg) from err
        except UfanetApiError as err:
            msg = f"Failed to open intercom: {err}"
            raise HomeAssistantError(msg) from err

        if not opened:
            msg = "Intercom did not confirm open"
            raise HomeAssistantError(msg)
