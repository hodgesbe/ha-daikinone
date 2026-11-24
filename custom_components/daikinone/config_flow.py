"""Account setup and reauthentication for Daikin One."""

from typing import Any

import probatio
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client.client import DaikinOne
from .client.models import DaikinUserCredentials
from .const import CONF_OPTION_ENTITY_UID_SCHEMA_VERSION_KEY, DOMAIN
from .exceptions import DaikinAuthError, DaikinServiceException


class DaikinOneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up a Skyport account."""

    VERSION = 1
    MINOR_VERSION = 2

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            if any(
                entry.data[CONF_EMAIL].strip().casefold() == email.casefold() for entry in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")
            password = user_input[CONF_PASSWORD]
            if error := await self._check_account(email, password):
                errors["base"] = error
            else:
                await self.async_set_unique_id(email.casefold())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Daikin One",
                    data={CONF_EMAIL: email, CONF_PASSWORD: password, CONF_OPTION_ENTITY_UID_SCHEMA_VERSION_KEY: 1},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_EMAIL): str,
                    probatio.Required(probatio.Secret(CONF_PASSWORD)): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            if error := await self._check_account(entry.data[CONF_EMAIL], password):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_PASSWORD: password})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema({probatio.Required(probatio.Secret(CONF_PASSWORD)): str}),
            errors=errors,
        )

    async def _check_account(self, email: str, password: str) -> str | None:
        client = DaikinOne(DaikinUserCredentials(email, password), async_get_clientsession(self.hass))
        try:
            await client.update()
        except DaikinAuthError:
            return "auth_failed"
        except DaikinServiceException:
            return "cannot_connect"
        return None
