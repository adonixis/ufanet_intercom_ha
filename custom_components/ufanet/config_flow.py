"""Config flow for the Ufanet Intercom integration."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from homeassistant.helpers.storage import Store

from .api import UfanetApiAuthError, UfanetApiClient, UfanetApiError
from .const import CONF_CONTRACT, CONF_PASSWORD, DOMAIN

STORAGE_KEY = f"{DOMAIN}_credentials"
STORAGE_VERSION = 1

_LOGGER = logging.getLogger(__name__)


class UfanetIntercomConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ufanet Intercom."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step: ask for credentials and validate them."""
        errors: dict[str, str] = {}

        if user_input is not None:
            contract = user_input[CONF_CONTRACT]
            password = user_input[CONF_PASSWORD]

            _LOGGER.debug("Starting authentication for contract: %s", contract)

            await self.async_set_unique_id(contract)
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = UfanetApiClient(session, contract, password=password)
            
            # Store for saving token
            store = Store(self.hass, STORAGE_VERSION, STORAGE_KEY)
            stored_data = await store.async_load() or {}
            refresh_token = None
            token_exp = None
            
            async def save_token(token: str, exp: int) -> None:
                nonlocal refresh_token, token_exp
                refresh_token = token
                token_exp = exp
                if contract not in stored_data:
                    stored_data[contract] = {}
                stored_data[contract]["refresh_token"] = token
                stored_data[contract]["refresh_exp"] = exp
                # Also save password for re-authentication if refresh token expires
                stored_data[contract]["password"] = password
                await store.async_save(stored_data)
            
            try:
                _LOGGER.debug("Requesting intercom list")
                intercoms = await client.async_get_intercoms(on_token_update=save_token)
                _LOGGER.debug("Fetched %s intercoms", len(intercoms))

                if not intercoms:
                    errors["base"] = "no_intercoms"
                else:
                    # Save all intercoms as a list
                    intercoms_data = [
                        {
                            "id": intercom.id,
                            "name": intercom.role_name or intercom.string_view or intercom.custom_name or f"Intercom {intercom.id}",
                        }
                        for intercom in intercoms
                    ]
                    
                    # Create entry with contract and intercoms (no password/token in entry.data)
                    data = {
                        CONF_CONTRACT: contract,
                        "intercoms": intercoms_data,
                    }
                    return self.async_create_entry(title=contract, data=data)
            except UfanetApiAuthError as err:  # explicit auth errors
                _LOGGER.warning("Authentication failed: %s", err)
                errors["base"] = "auth"
            except UfanetApiError as err:  # other API errors
                _LOGGER.error("API error: %s", err)
                errors["base"] = "unknown"
            except Exception as err:  # pragma: no cover - bubble to UI
                _LOGGER.error("Error validating credentials", exc_info=True)
                _LOGGER.error("Exception type: %s, message: %s", type(err).__name__, str(err))

                # Extract error message - could be dict, list, or string
                error_msg = str(err)
                if isinstance(err.args[0] if err.args else None, dict):
                    # Try to extract message from dict (e.g., {'non_field_errors': [...]})
                    error_dict = err.args[0]
                    if "non_field_errors" in error_dict:
                        error_list = error_dict["non_field_errors"]
                        if error_list:
                            error_msg = " ".join(str(e) for e in error_list)
                    else:
                        error_msg = str(error_dict)
                elif err.args:
                    error_msg = str(err.args[0])

                error_msg_lower = error_msg.lower()

                # Check exception type name
                exception_name = type(err).__name__.lower()

                # Explicit auth errors
                if "unauthorized" in exception_name:
                    _LOGGER.warning("Unauthorized error: %s", error_msg)
                    errors["base"] = "auth"
                # Timeout/unknown errors - check if message indicates auth failure
                elif "timeout" in exception_name or "unknown" in exception_name:
                    auth_keywords = [
                        "невозможно войти",
                        "учетными данными",
                        "неверный",
                        "неправильный",
                        "invalid",
                        "auth",
                        "login",
                        "password",
                        "unauthorized",
                        "forbidden",
                        "401",
                        "403",
                        "decoding signature",
                        "error decoding",
                    ]
                    if any(keyword in error_msg_lower for keyword in auth_keywords):
                        _LOGGER.warning("Authentication failed: %s", error_msg)
                        errors["base"] = "auth"
                    else:
                        errors["base"] = "unknown"
                # Other exceptions - check message for auth-related keywords
                else:
                    auth_keywords = [
                        "auth",
                        "login",
                        "password",
                        "unauthorized",
                        "forbidden",
                        "401",
                        "403",
                        "timeout",
                        "невозможно войти",
                        "учетными данными",
                        "decoding signature",
                        "error decoding",
                    ]
                    # Also check if error dict contains 'detail' with auth-related message
                    if isinstance(err.args[0] if err.args else None, dict):
                        error_dict = err.args[0]
                        if "detail" in error_dict:
                            detail_msg = str(error_dict["detail"]).lower()
                            if any(keyword in detail_msg for keyword in auth_keywords):
                                errors["base"] = "auth"
                            else:
                                errors["base"] = "unknown"
                    elif any(keyword in error_msg_lower for keyword in auth_keywords):
                        errors["base"] = "auth"
                    else:
                        errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CONTRACT): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )



