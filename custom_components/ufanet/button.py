"""Button platform for Ufanet Intercom to open the selected intercom."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo

from . import create_token_save_callback, get_password_from_storage
from .api import (
    UfanetApiAuthError,
    UfanetApiClient,
    UfanetApiError,
    intercom_display_name,
)
from .const import CONF_CONTRACT, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up button entities for all intercoms."""
    data = hass.data[DOMAIN][entry.entry_id]
    session = async_get_clientsession(hass)

    store = data.get("_store")
    contract = data.get("_contract")
    save_token = create_token_save_callback(store, contract)

    password = await get_password_from_storage(store, contract)

    client = UfanetApiClient(
        session,
        data[CONF_CONTRACT],
        password=password,
        refresh_token=data.get("refresh_token"),
        refresh_exp=data.get("refresh_exp"),
    )

    try:
        intercoms = await client.async_get_intercoms(on_token_update=save_token)
    except UfanetApiAuthError:
        intercoms = []
    except UfanetApiError:
        intercoms = []
    except Exception:  # noqa: BLE001
        intercoms = []

    if not intercoms:
        return

    intercoms_data = [
        {"id": intercom.id, "name": intercom_display_name(intercom)}
        for intercom in intercoms
    ]

    buttons = [UfanetOpenDoorButton(entry, data, intercom) for intercom in intercoms_data]
    async_add_entities(buttons, update_before_add=True)


class UfanetOpenDoorButton(ButtonEntity):
    """Button to open a specific intercom."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, data: dict, intercom: dict) -> None:
        """Initialize the button entity."""
        self._entry = entry
        self._contract = data[CONF_CONTRACT]
        self._intercom_id = intercom["id"]
        self._intercom_name = intercom["name"]

        self.entity_description = ButtonEntityDescription(
            key=f"open_intercom_{self._intercom_id}",
            translation_key="open_intercom",
            icon="mdi:door",
        )

        self._attr_unique_id = f"{self._contract}_{self._intercom_id}"
        self.internal_integration_suggested_object_id = (
            f"{self._contract}_{self._intercom_id}"
        )
        self._attr_name = self._intercom_name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._contract)},
            name=self._contract,
            manufacturer="Ufanet",
        )

    async def async_press(self) -> None:
        """Handle the button press to open the intercom."""
        data = self.hass.data[DOMAIN][self._entry.entry_id]
        session = async_get_clientsession(self.hass)

        store = data.get("_store")
        contract = data.get("_contract")
        save_token = create_token_save_callback(store, contract)

        password = await get_password_from_storage(store, contract)

        client = UfanetApiClient(
            session,
            self._contract,
            password=password,
            refresh_token=data.get("refresh_token"),
            refresh_exp=data.get("refresh_exp"),
        )
        await client.async_open_intercom(self._intercom_id, on_token_update=save_token)
