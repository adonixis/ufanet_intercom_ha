"""Constants for the Ufanet Intercom integration."""

DOMAIN = "ufanet"

CONF_CONTRACT = "contract"
CONF_PASSWORD = "password"
CONF_INTERCOM_ID = "intercom_id"

# Auth method keys for config flow.
CONF_AUTH_METHOD = "auth_method"
CONF_PHONE = "phone"
CONF_CONTRACT_ID = "contract_id"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_DOM_URL = "dom_url"
AUTH_PASSWORD = "password"
AUTH_PHONE = "phone"

PHONE_APPLICATION_ID = "ru.ufanet.smarthome"
PHONE_COUNTRY_ID = 1

DEFAULT_DOM_URL = "https://dom.ufanet.ru/"

STORAGE_KEY = f"{DOMAIN}_credentials"
STORAGE_VERSION = 1
