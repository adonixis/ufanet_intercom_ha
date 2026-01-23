"""Button platform for Ufanet Intercom to open the selected intercom."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import UfanetApiClient
from .const import CONF_CONTRACT, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up button entities for all intercoms."""
    data = hass.data[DOMAIN][entry.entry_id]
    intercoms = data.get("intercoms", [])
    
    buttons = [
        UfanetOpenDoorButton(entry, data, intercom)
        for intercom in intercoms
    ]
    async_add_entities(buttons)


class UfanetOpenDoorButton(ButtonEntity):
    """Button to open a specific intercom."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, data: dict, intercom: dict) -> None:
        self._entry = entry
        self._contract = data[CONF_CONTRACT]
        self._intercom_id = intercom["id"]
        self._intercom_name = intercom["name"]

        self.entity_description = ButtonEntityDescription(
            key=f"open_intercom_{self._intercom_id}",
            translation_key="open_intercom",
            icon="mdi:door",
        )

        self._attr_unique_id = f"{self._contract}_{self._intercom_id}_open"
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
            self._contract,
            password=password,
            refresh_token=data.get("refresh_token"),
            refresh_exp=data.get("refresh_exp"),
        )
        await client.async_open_intercom(self._intercom_id, on_token_update=save_token)


