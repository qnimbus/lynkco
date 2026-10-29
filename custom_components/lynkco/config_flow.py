"""Config flow for Lynk & Co integration."""

import logging
import uuid

import aiohttp
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigFlow,
    OptionsFlow,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import LynkCoAPI, LynkCoAuthError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ID,
    CONF_DRIVING_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    DRIVING_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

PASTE_SCHEMA = vol.Schema({vol.Required("redirect_url"): str})


class LynkCoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Lynk & Co."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return LynkCoOptionsFlow(config_entry)

    def __init__(self) -> None:
        """Initialize."""
        self._auth_url: str | None = None
        self._code_verifier: str | None = None

    def _new_login_link(self) -> None:
        """Create a login URL with a fresh PKCE pair."""
        self._auth_url, self._code_verifier, _ = LynkCoAPI.generate_auth_url()

    def _show_paste_form(self, errors: dict[str, str], login_error: str = ""):
        return self.async_show_form(
            step_id="paste_redirect",
            data_schema=PASTE_SCHEMA,
            errors=errors,
            # The link is repeated here so a failed attempt can be retried
            # without restarting the flow.
            description_placeholders={
                "auth_url": self._auth_url,
                "login_error": login_error,
            },
        )

    async def async_step_user(self, user_input=None):
        """Step 1: Generate auth URL and show it to the user."""
        self._new_login_link()
        return self.async_show_form(
            step_id="auth_url",
            description_placeholders={"auth_url": self._auth_url},
            data_schema=vol.Schema({}),
        )

    async def async_step_auth_url(self, user_input=None):
        """Step 2: User has opened the URL — ask for the redirect."""
        return self._show_paste_form({})

    async def async_step_paste_redirect(self, user_input=None):
        """Step 3: Exchange the code for tokens."""
        if user_input is None:
            return self._show_paste_form({})

        code, login_error = LynkCoAPI.parse_redirect(user_input["redirect_url"])
        if login_error:
            return self._show_paste_form({"redirect_url": "login_error"}, login_error)
        if not code:
            return self._show_paste_form({"redirect_url": "no_code"})

        session = async_get_clientsession(self.hass)
        try:
            tokens = await LynkCoAPI.exchange_code(session, code, self._code_verifier)
        except (aiohttp.ClientError, TimeoutError) as err:
            # The code was never used, so it may still work on a retry.
            _LOGGER.warning("Could not reach the Lynk & Co login service: %s", err)
            return self._show_paste_form({"base": "cannot_connect"})

        if tokens is None:
            # Codes are single-use and expire after a few minutes. A new link
            # with a fresh PKCE pair lets the user log in again straight away.
            self._new_login_link()
            return self._show_paste_form({"redirect_url": "exchange_failed"})

        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        device_id = str(uuid.uuid4())
        if not access_token:
            return self._show_paste_form({"redirect_url": "no_token"})

        # Validate by calling the API
        api = LynkCoAPI(session, access_token, refresh_token, device_id)
        try:
            if not await api.validate_session():
                return self._show_paste_form({"redirect_url": "session_failed"})
            vehicles = await api.get_vehicles()
        except LynkCoAuthError:
            return self._show_paste_form({"redirect_url": "session_failed"})
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("Could not reach the Lynk & Co API: %s", err)
            return self._show_paste_form({"base": "cannot_connect"})
        if not vehicles:
            return self._show_paste_form({"redirect_url": "no_vehicles"})

        # Use email as unique ID
        await self.async_set_unique_id(api.user_email)
        data = {
            CONF_ACCESS_TOKEN: access_token,
            CONF_REFRESH_TOKEN: refresh_token,
            CONF_DEVICE_ID: device_id,
        }

        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            # Re-login: keep the entry (and its entities), only swap the tokens.
            # The abort reason follows the source (reauth_/reconfigure_successful).
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            entry = (
                self._get_reauth_entry()
                if self.source == SOURCE_REAUTH
                else self._get_reconfigure_entry()
            )
            return self.async_update_reload_and_abort(entry, data_updates=data)

        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"Lynk & Co ({api.user_email})", data=data)

    async def async_step_reauth(self, entry_data=None):
        """Handle re-authentication (opened from the Repairs entry).

        Same browser login as the initial setup; the existing entry is updated
        with the new tokens in async_step_paste_redirect.
        """
        return await self.async_step_user()

    async def async_step_reconfigure(self, user_input=None):
        """Log in again on demand (the entry's ⋮ menu > Reconfigure).

        For example after logging in on the phone app, which ends the Home
        Assistant session. Same browser login as the initial setup.
        """
        return await self.async_step_user()


class LynkCoOptionsFlow(OptionsFlow):
    """Handle options for Lynk & Co."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self._config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL // 60),
                ): vol.All(vol.Coerce(int), vol.Range(min=15, max=180)),
                vol.Required(
                    CONF_DRIVING_INTERVAL,
                    default=current.get(CONF_DRIVING_INTERVAL, DRIVING_SCAN_INTERVAL // 60),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
            }),
        )
