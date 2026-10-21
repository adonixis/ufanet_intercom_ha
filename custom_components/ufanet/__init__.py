"""The Ufanet Intercom integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import IntercomInfo, UfanetApiAuthError, UfanetApiClient, UfanetApiError
from .const import (
    AUTH_PASSWORD,
    AUTH_PHONE,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_CONTRACT,
    CONF_DOM_URL,
    CONF_REFRESH_TOKEN,
    DEFAULT_DOM_URL,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.CAMERA]

type UfanetConfigEntry = ConfigEntry[UfanetRuntimeData]


@dataclass
class UfanetRuntimeData:
    """Runtime objects for a config entry."""

    client: UfanetApiClient
    store: Store
    contract: str
    intercoms: list[IntercomInfo]


async def _save_tokens(
    store: Store,
    contract: str,
    token: str,
    exp: int | None,
    *,
    access_token: str | None = None,
) -> None:
    """Persist rotated tokens in Home Assistant Store (not entry.data)."""
    stored_data = await store.async_load() or {}
    creds = dict(stored_data.get(contract) or {})
    creds["refresh_token"] = token
    creds["refresh_exp"] = exp
    if access_token:
        creds["access_token"] = access_token
    stored_data[contract] = creds
    await store.async_save(stored_data)


async def _load_credentials(store: Store, contract: str, entry: ConfigEntry) -> dict:
    """Load credentials from Store, falling back to legacy entry.data tokens."""
    stored_data = await store.async_load() or {}
    credentials = dict(stored_data.get(contract) or {})

    # Migrate tokens previously stored in entry.data into Store.
    legacy_refresh = entry.data.get(CONF_REFRESH_TOKEN)
    legacy_access = entry.data.get(CONF_ACCESS_TOKEN)
    changed = False
    if legacy_refresh and not credentials.get("refresh_token"):
        credentials["refresh_token"] = legacy_refresh
        changed = True
    if legacy_access and not credentials.get("access_token"):
        credentials["access_token"] = legacy_access
        changed = True

    refresh_token = credentials.get("refresh_token")
    if refresh_token:
        jwt_exp = UfanetApiClient.extract_exp(refresh_token)
        if jwt_exp and jwt_exp != credentials.get("refresh_exp"):
            credentials["refresh_exp"] = jwt_exp
            changed = True

    if changed:
        stored_data[contract] = credentials
        await store.async_save(stored_data)

    return credentials


def _entry_data_without_secrets(entry: ConfigEntry) -> dict:
    """Return entry data with JWT secrets stripped."""
    data = dict(entry.data)
    data.pop(CONF_ACCESS_TOKEN, None)
    data.pop(CONF_REFRESH_TOKEN, None)
    return data


async def async_setup(_hass: HomeAssistant, _config: dict) -> bool:
    """Set up the integration from yaml (not supported)."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries."""
    _LOGGER.debug("Migrating %s from version %s", entry.entry_id, entry.version)
    version_auth_method = 2
    version_store_tokens = 3

    if entry.version < version_auth_method:
        data = dict(entry.data)
        data.setdefault(CONF_AUTH_METHOD, AUTH_PASSWORD)
        data.setdefault(CONF_DOM_URL, DEFAULT_DOM_URL)
        hass.config_entries.async_update_entry(
            entry, data=data, version=version_auth_method
        )

    if entry.version < version_store_tokens:
        # Move JWT out of entry.data into Store; keep only non-secret fields.
        contract = entry.data.get(CONF_CONTRACT)
        if contract:
            store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
            await _load_credentials(store, contract, entry)
        hass.config_entries.async_update_entry(
            entry,
            data=_entry_data_without_secrets(entry),
            version=version_store_tokens,
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: UfanetConfigEntry) -> bool:
    """Set up Ufanet Intercom from a config entry."""
    contract = entry.data.get(CONF_CONTRACT)
    if not contract:
        msg = "Missing contract number"
        raise ConfigEntryAuthFailed(msg)

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    credentials = await _load_credentials(store, contract, entry)

    # Strip secrets from entry.data if still present (pre-migration reload).
    if CONF_ACCESS_TOKEN in entry.data or CONF_REFRESH_TOKEN in entry.data:
        hass.config_entries.async_update_entry(
            entry, data=_entry_data_without_secrets(entry)
        )

    auth_method = entry.data.get(CONF_AUTH_METHOD, AUTH_PASSWORD)
    is_phone = auth_method == AUTH_PHONE
    password = None if is_phone else credentials.get("password")

    if not is_phone and not password and not credentials.get("refresh_token"):
        msg = "Missing credentials; please reauthenticate"
        raise ConfigEntryAuthFailed(msg)

    if is_phone and not credentials.get("refresh_token"):
        msg = "Phone session expired; please reauthenticate"
        raise ConfigEntryAuthFailed(msg)

    session = async_get_clientsession(hass)
    client = UfanetApiClient(
        session,
        contract,
        password=password,
        access_token=credentials.get("access_token"),
        refresh_token=credentials.get("refresh_token"),
        refresh_exp=credentials.get("refresh_exp"),
        base_url=entry.data.get(CONF_DOM_URL) or DEFAULT_DOM_URL,
        allow_password_login=not is_phone,
    )

    async def on_token_update(token: str, exp: int | None) -> None:
        await _save_tokens(store, contract, token, exp)

    client.set_token_update_callback(on_token_update)

    try:
        intercoms = await client.async_get_intercoms()
    except UfanetApiAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except UfanetApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    if not intercoms:
        msg = "No intercoms available for this account"
        raise ConfigEntryNotReady(msg)

    # Account hub device; platforms attach via_device.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, contract)},
        manufacturer="Ufanet",
        name=f"Ufanet {contract}",
        model="Intercom account",
    )

    entry.runtime_data = UfanetRuntimeData(
        client=client,
        store=store,
        contract=contract,
        intercoms=intercoms,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UfanetConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry - clean up secure storage."""
    contract = entry.data.get(CONF_CONTRACT)
    if contract:
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        stored_data = await store.async_load() or {}
        stored_data.pop(contract, None)
        await store.async_save(stored_data)
