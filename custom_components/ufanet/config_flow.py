"""Config flow for the Ufanet Intercom integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import UfanetApiAuthError, UfanetApiClient, UfanetApiError
from .const import CONF_CONTRACT, CONF_PASSWORD, DOMAIN

if TYPE_CHECKING:
    from homeassistant.data_entry_flow import FlowResult

STORAGE_KEY = f"{DOMAIN}_credentials"
STORAGE_VERSION = 1


class UfanetIntercomConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ufanet Intercom."""

    VERSION = 1

    async def async_step_user(  # noqa: PLR0912, PLR0915
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step: ask for credentials and validate them."""
        errors: dict[str, str] = {}

        if user_input is not None:
            contract = user_input[CONF_CONTRACT]
            password = user_input[CONF_PASSWORD]

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
                intercoms = await client.async_get_intercoms(on_token_update=save_token)

                if not intercoms:
                    errors["base"] = "no_intercoms"
                else:
                    # Save all intercoms as a list
                    intercoms_data = []
                    for intercom in intercoms:
                        name = (
                            intercom.role_name
                            or intercom.string_view
                            or intercom.custom_name
                            or f"Intercom {intercom.id}"
                        )
                        intercoms_data.append({"id": intercom.id, "name": name})

                    # Create entry with contract and intercoms
                    # (no password/token in entry.data)
                    data = {
                        CONF_CONTRACT: contract,
                        "intercoms": intercoms_data,
                    }
                    return self.async_create_entry(title=contract, data=data)
            except UfanetApiAuthError:  # explicit auth errors
                errors["base"] = "auth"
            except UfanetApiError:  # other API errors
                errors["base"] = "unknown"
            except Exception as err:  # noqa: BLE001  # pragma: no cover - bubble to UI
                # Extract error message - could be dict, list, or string
                error_msg = str(err)
                first_arg = err.args[0] if err.args else None
                if isinstance(first_arg, dict):
                    # Try to extract message from dict
                    # (e.g., {'non_field_errors': [...]})
                    error_dict = first_arg
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
                    # Also check if error dict contains 'detail'
                    # with auth-related message
                    first_arg = err.args[0] if err.args else None
                    if isinstance(first_arg, dict):
                        error_dict = first_arg
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
