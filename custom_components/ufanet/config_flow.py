"""
Config flow for the Ufanet Intercom integration.

Auth UX: menu with
contract+password or phone-call login for accounts without a password.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import (
    PhoneAuthError,
    UfanetApiAuthError,
    UfanetApiClient,
    UfanetApiError,
    intercom_display_name,
    phone_auth_apply,
    phone_auth_contact_list,
    phone_auth_init,
)
from .const import (
    AUTH_PASSWORD,
    AUTH_PHONE,
    CONF_AUTH_METHOD,
    CONF_CONTRACT,
    CONF_CONTRACT_ID,
    CONF_DOM_URL,
    CONF_PASSWORD,
    CONF_PHONE,
    DEFAULT_DOM_URL,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.data_entry_flow import FlowResult

_LOGGER = logging.getLogger(__name__)

_CONTACT_LIST_POLLS = 5
_CONTACT_LIST_DELAY = 2

AUTH_ERROR_KEYWORDS = [
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
    "неверный",
    "неправильный",
    "invalid",
    "decoding signature",
    "error decoding",
]

PASSWORD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CONTRACT): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_DOM_URL, default=DEFAULT_DOM_URL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
        ),
    }
)

REAUTH_PASSWORD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)

PHONE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PHONE): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEL)
        ),
        vol.Optional(CONF_DOM_URL, default=DEFAULT_DOM_URL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
        ),
    }
)


def _is_auth_error(error_msg: str, exception_name: str) -> bool:
    if "unauthorized" in exception_name:
        return True
    return any(keyword in error_msg for keyword in AUTH_ERROR_KEYWORDS)


def _normalize_dom_url(url: str | None) -> str:
    dom_url = (url or DEFAULT_DOM_URL).strip()
    if not dom_url.endswith("/"):
        dom_url += "/"
    return dom_url


def _contract_label(contract: dict) -> str:
    title = contract.get("title") or str(contract.get("id"))
    addresses = contract.get("addresses") or []
    if addresses:
        views = ", ".join(
            a.get("string_view", "") for a in addresses if a.get("string_view")
        )
        if views:
            return f"{title} ({views})"
    return title


async def _persist_password_credentials(
    hass: Any,
    contract: str,
    password: str,
    refresh_token: str | None,
    refresh_exp: int | None,
) -> None:
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_data = await store.async_load() or {}
    creds = dict(stored_data.get(contract) or {})
    creds["password"] = password
    if refresh_token:
        creds["refresh_token"] = refresh_token
        creds["refresh_exp"] = refresh_exp
    stored_data[contract] = creds
    await store.async_save(stored_data)


async def _persist_phone_tokens(
    hass: Any,
    contract_id: str,
    access: str,
    refresh: str,
    refresh_exp: int | None,
) -> None:
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_data = await store.async_load() or {}
    stored_data[contract_id] = {
        "refresh_token": refresh,
        "refresh_exp": refresh_exp,
        "access_token": access,
    }
    await store.async_save(stored_data)


class UfanetIntercomConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ufanet Intercom."""

    VERSION = 3

    def __init__(self) -> None:
        """Initialize ephemeral flow state."""
        self._phone_ctx: dict[str, Any] = {}
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, _user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose auth method (password or phone)."""
        return self.async_show_menu(step_id="user", menu_options=["password", "phone"])

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start reauthentication when credentials expire."""
        self._reauth_entry = self._get_reauth_entry()
        auth_method = entry_data.get(CONF_AUTH_METHOD, AUTH_PASSWORD)
        if auth_method == AUTH_PHONE:
            return await self.async_step_reauth_phone()
        return await self.async_step_reauth_password()

    async def async_step_reauth_password(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Reauthenticate with contract password."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="unknown")
        contract = str(entry.data[CONF_CONTRACT])
        dom_url = _normalize_dom_url(entry.data.get(CONF_DOM_URL))

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            session = async_get_clientsession(self.hass)
            client = UfanetApiClient(
                session, contract, password=password, base_url=dom_url
            )

            async def save_token(token: str, exp: int | None) -> None:
                await _persist_password_credentials(
                    self.hass, contract, password, token, exp
                )

            client.set_token_update_callback(save_token)

            try:
                intercoms = await client.async_get_intercoms()
            except UfanetApiAuthError:
                errors["base"] = "auth"
            except UfanetApiError:
                errors["base"] = "unknown"
            except Exception as err:
                error_msg = str(err.args[0] if err.args else err).lower()
                if _is_auth_error(error_msg, type(err).__name__.lower()):
                    errors["base"] = "auth"
                else:
                    _LOGGER.exception("Unexpected error during Ufanet reauth")
                    errors["base"] = "unknown"
            else:
                if not intercoms:
                    errors["base"] = "no_intercoms"
                else:
                    await _persist_password_credentials(
                        self.hass,
                        contract,
                        password,
                        None,
                        None,
                    )
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_AUTH_METHOD: AUTH_PASSWORD,
                            CONF_DOM_URL: dom_url,
                        },
                    )

        return self.async_show_form(
            step_id="reauth_password",
            data_schema=REAUTH_PASSWORD_SCHEMA,
            description_placeholders={"contract": contract},
            errors=errors,
        )

    async def async_step_reauth_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Reauthenticate via phone call for password-less accounts."""
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="unknown")
        errors: dict[str, str] = {}
        default_url = entry.data.get(CONF_DOM_URL) or DEFAULT_DOM_URL

        if user_input is not None:
            base_url = _normalize_dom_url(user_input.get(CONF_DOM_URL) or default_url)
            phone = str(user_input[CONF_PHONE]).strip()
            session = async_get_clientsession(self.hass)
            try:
                init = await phone_auth_init(session, base_url, phone)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("phone_auth init failed: %r", err)
                errors["base"] = "phone_init_failed"
            else:
                self._phone_ctx = {
                    "base_url": base_url,
                    "request_id": init["request_id"],
                    "phone_to_call": init["phone_to_call"],
                    "phone": phone,
                    "reauth": True,
                    "expected_contract": str(entry.data[CONF_CONTRACT]),
                }
                return await self.async_step_phone_confirm()

        return self.async_show_form(
            step_id="reauth_phone",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PHONE): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.TEL)
                    ),
                    vol.Optional(
                        CONF_DOM_URL, default=default_url
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Contract number + password login."""
        errors: dict[str, str] = {}

        if user_input is not None:
            contract = str(user_input[CONF_CONTRACT]).strip()
            password = user_input[CONF_PASSWORD]
            dom_url = _normalize_dom_url(user_input.get(CONF_DOM_URL))

            await self.async_set_unique_id(contract)
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = UfanetApiClient(
                session, contract, password=password, base_url=dom_url
            )

            async def save_token(token: str, exp: int | None) -> None:
                await _persist_password_credentials(
                    self.hass, contract, password, token, exp
                )

            client.set_token_update_callback(save_token)

            try:
                intercoms = await client.async_get_intercoms()
                if not intercoms:
                    errors["base"] = "no_intercoms"
                else:
                    await _persist_password_credentials(
                        self.hass, contract, password, None, None
                    )
                    intercoms_data = [
                        {
                            "id": intercom.id,
                            "name": intercom_display_name(intercom),
                        }
                        for intercom in intercoms
                    ]
                    return self.async_create_entry(
                        title=contract,
                        data={
                            CONF_CONTRACT: contract,
                            CONF_AUTH_METHOD: AUTH_PASSWORD,
                            CONF_DOM_URL: dom_url,
                            "intercoms": intercoms_data,
                        },
                    )
            except UfanetApiAuthError:
                errors["base"] = "auth"
            except UfanetApiError:
                errors["base"] = "unknown"
            except Exception as err:
                error_msg = str(err.args[0] if err.args else err).lower()
                if _is_auth_error(error_msg, type(err).__name__.lower()):
                    errors["base"] = "auth"
                else:
                    _LOGGER.exception("Unexpected error during Ufanet password auth")
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="password",
            data_schema=PASSWORD_SCHEMA,
            errors=errors,
        )

    async def async_step_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start phone-call auth (accounts without a local password)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = _normalize_dom_url(user_input.get(CONF_DOM_URL))
            phone = str(user_input[CONF_PHONE]).strip()
            session = async_get_clientsession(self.hass)
            try:
                init = await phone_auth_init(session, base_url, phone)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("phone_auth init failed: %r", err)
                errors["base"] = "phone_init_failed"
            else:
                self._phone_ctx = {
                    "base_url": base_url,
                    "request_id": init["request_id"],
                    "phone_to_call": init["phone_to_call"],
                    "phone": phone,
                    "reauth": False,
                }
                return await self.async_step_phone_confirm()

        return self.async_show_form(
            step_id="phone",
            data_schema=PHONE_SCHEMA,
            errors=errors,
        )

    async def async_step_phone_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Wait until Ufanet registers the incoming confirmation call."""
        ctx = self._phone_ctx
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            contracts: list[dict[str, Any]] = []
            for _ in range(_CONTACT_LIST_POLLS):
                try:
                    contracts = await phone_auth_contact_list(
                        session, ctx["base_url"], ctx["request_id"]
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("phone_auth contact_list failed: %r", err)
                    errors["base"] = "phone_call_failed"
                    break
                if contracts:
                    break
                await asyncio.sleep(_CONTACT_LIST_DELAY)

            if not errors:
                if not contracts:
                    errors["base"] = "phone_call_not_detected"
                elif len(contracts) == 1:
                    return await self._finish_phone(contracts[0])
                else:
                    ctx["contracts"] = {
                        str(c["id"]): _contract_label(c) for c in contracts
                    }
                    return await self.async_step_phone_contract()

        return self.async_show_form(
            step_id="phone_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"phone_to_call": ctx.get("phone_to_call", "")},
            errors=errors,
        )

    async def async_step_phone_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick a contract when the phone is tied to several accounts."""
        ctx = self._phone_ctx
        if user_input is not None:
            return await self._finish_phone({"id": int(user_input[CONF_CONTRACT_ID])})
        return self.async_show_form(
            step_id="phone_contract",
            data_schema=vol.Schema(
                {vol.Required(CONF_CONTRACT_ID): vol.In(ctx["contracts"])}
            ),
        )

    async def _finish_phone(self, contract: dict[str, Any]) -> FlowResult:
        ctx = self._phone_ctx
        contract_id = str(contract["id"])
        session = async_get_clientsession(self.hass)

        if ctx.get("reauth"):
            expected = ctx.get("expected_contract")
            if expected and contract_id != expected:
                return self.async_abort(reason="wrong_account")

        try:
            tokens = await phone_auth_apply(
                session, ctx["base_url"], ctx["request_id"], int(contract_id)
            )
        except (PhoneAuthError, Exception) as err:  # noqa: BLE001
            _LOGGER.warning("phone_auth apply failed: %r", err)
            return self.async_abort(reason="phone_apply_failed")

        refresh = tokens["refresh"]
        refresh_exp = UfanetApiClient.extract_exp(refresh)
        await _persist_phone_tokens(
            self.hass,
            contract_id,
            tokens["access"],
            refresh,
            refresh_exp,
        )

        client = UfanetApiClient(
            session,
            contract_id,
            access_token=tokens["access"],
            refresh_token=refresh,
            refresh_exp=refresh_exp,
            base_url=ctx["base_url"],
            allow_password_login=False,
        )

        async def save_token(token: str, exp: int | None) -> None:
            await _persist_phone_tokens(
                self.hass, contract_id, tokens["access"], token, exp
            )

        client.set_token_update_callback(save_token)

        try:
            intercoms = await client.async_get_intercoms()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("phone auth: failed to list intercoms: %r", err)
            return self.async_abort(reason="unknown")

        if not intercoms:
            return self.async_abort(reason="no_intercoms")

        intercoms_data = [
            {"id": i.id, "name": intercom_display_name(i)} for i in intercoms
        ]
        entry_data = {
            CONF_CONTRACT: contract_id,
            CONF_AUTH_METHOD: AUTH_PHONE,
            CONF_DOM_URL: ctx["base_url"],
            CONF_CONTRACT_ID: contract_id,
            "intercoms": intercoms_data,
        }

        if ctx.get("reauth") and self._reauth_entry:
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data_updates=entry_data,
            )

        await self.async_set_unique_id(contract_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=contract_id,
            data=entry_data,
        )
