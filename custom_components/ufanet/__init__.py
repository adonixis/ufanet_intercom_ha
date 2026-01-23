"""The Ufanet Intercom integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import CONF_CONTRACT, DOMAIN

STORAGE_KEY = f"{DOMAIN}_credentials"
STORAGE_VERSION = 1

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.CAMERA]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration from yaml (not supported)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ufanet Intercom from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    # Load credentials from secure storage (keyed by contract)
    contract = entry.data.get(CONF_CONTRACT)
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_data = await store.async_load() or {}
    credentials = stored_data.get(contract, {})
    
    # Prepare data for platforms (credentials from secure storage, rest from entry.data)
    platform_data = dict(entry.data)
    # Add credentials from secure storage
    platform_data["refresh_token"] = credentials.get("refresh_token")
    platform_data["refresh_exp"] = credentials.get("refresh_exp")
    platform_data["_store"] = store
    platform_data["_contract"] = contract
    
    hass.data[DOMAIN][entry.entry_id] = platform_data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry - clean up secure storage."""
    contract = entry.data.get(CONF_CONTRACT)
    if contract:
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        stored_data = await store.async_load() or {}
        stored_data.pop(contract, None)
        await store.async_save(stored_data)


