"""Config flow for Emaldo integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback

from .emaldo_lib import EmaldoClient, EmaldoAuthError

from .const import (
    DOMAIN,
    CONF_HOME_ID,
    CONF_DEVICE_ID,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_APP_VERSION,
    DEFAULT_APP_ID,
    DEFAULT_APP_SECRET,
    DEFAULT_APP_VERSION,
    CONF_SCHEDULE_START_HOUR,
    CONF_SCHEDULE_START_MINUTE,
    CONF_SCHEDULE_INTERVAL,
    CONF_REALTIME_STREAM_MODE,
    REALTIME_STREAM_MODE,
    DEFAULT_SCHEDULE_START_HOUR,
    DEFAULT_SCHEDULE_START_MINUTE,
    DEFAULT_SCHEDULE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _select_device(devices: list[dict], preferred_id: str | None) -> dict[str, Any] | None:
    """Select configured device or fall back to the first discovered device."""
    if not devices:
        return None

    wanted = (preferred_id or "").strip()
    if not wanted:
        return devices[0]

    for device in devices:
        if str(device.get("id", "")) == wanted:
            return device
    return None

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_APP_ID, default=DEFAULT_APP_ID): str,
        vol.Required(CONF_APP_SECRET, default=DEFAULT_APP_SECRET): str,
        vol.Required(CONF_APP_VERSION, default=DEFAULT_APP_VERSION): str,
        vol.Optional(CONF_HOME_ID): str,
        vol.Optional(CONF_DEVICE_ID): str,
    }
)


class EmaldoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Emaldo."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return EmaldoOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                client = EmaldoClient(
                    app_id=user_input[CONF_APP_ID],
                    app_secret=user_input[CONF_APP_SECRET],
                    app_version=user_input[CONF_APP_VERSION],
                )
                await self.hass.async_add_executor_job(
                    client.login, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )

                # Resolve home_id: use provided or auto-discover
                home_id = user_input.get(CONF_HOME_ID, "").strip()
                if not home_id:
                    hid, _ = await self.hass.async_add_executor_job(client.find_home)
                    home_id = hid

                # Verify home has devices
                devices = await self.hass.async_add_executor_job(
                    client.list_devices, home_id
                )
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    selected = _select_device(
                        devices, user_input.get(CONF_DEVICE_ID)
                    )
                    if selected is None:
                        errors["base"] = "invalid_device"
                        return self.async_show_form(
                            step_id="user",
                            data_schema=STEP_USER_DATA_SCHEMA,
                            errors=errors,
                        )

                    # Use email as unique id
                    await self.async_set_unique_id(user_input[CONF_EMAIL])
                    self._abort_if_unique_id_configured()

                    return self.async_create_entry(
                        title=f"Emaldo ({user_input[CONF_EMAIL]})",
                        data={
                            CONF_EMAIL: user_input[CONF_EMAIL],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_HOME_ID: home_id,
                            CONF_DEVICE_ID: selected["id"],
                            CONF_DEVICE_MODEL: selected["model"],
                            CONF_DEVICE_NAME: selected.get("name", selected["id"]),
                            CONF_APP_ID: user_input[CONF_APP_ID],
                            CONF_APP_SECRET: user_input[CONF_APP_SECRET],
                            CONF_APP_VERSION: user_input[CONF_APP_VERSION],
                        },
                    )
            except EmaldoAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during Emaldo setup")
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow the user to update credentials and app parameters."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                client = EmaldoClient(
                    app_id=user_input[CONF_APP_ID],
                    app_secret=user_input[CONF_APP_SECRET],
                    app_version=user_input[CONF_APP_VERSION],
                )
                await self.hass.async_add_executor_job(
                    client.login, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )

                home_id = user_input.get(CONF_HOME_ID, "").strip()
                if not home_id:
                    hid, _ = await self.hass.async_add_executor_job(client.find_home)
                    home_id = hid

                devices = await self.hass.async_add_executor_job(
                    client.list_devices, home_id
                )
                selected = _select_device(
                    devices, user_input.get(CONF_DEVICE_ID)
                )
                if selected is None:
                    raise ValueError("invalid_device")

                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_HOME_ID: home_id,
                        CONF_DEVICE_ID: selected["id"],
                        CONF_DEVICE_MODEL: selected["model"],
                        CONF_DEVICE_NAME: selected.get("name", selected["id"]),
                        CONF_APP_ID: user_input[CONF_APP_ID],
                        CONF_APP_SECRET: user_input[CONF_APP_SECRET],
                        CONF_APP_VERSION: user_input[CONF_APP_VERSION],
                    },
                )
            except ValueError as err:
                if str(err) == "invalid_device":
                    errors["base"] = "invalid_device"
                else:
                    errors["base"] = "cannot_connect"
            except EmaldoAuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during Emaldo reconfigure")
                errors["base"] = "cannot_connect"

        current = entry.data
        reconfigure_schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL, default=current.get(CONF_EMAIL, "")): str,
                vol.Required(CONF_PASSWORD, default=current.get(CONF_PASSWORD, "")): str,
                vol.Required(
                    CONF_APP_ID, default=current.get(CONF_APP_ID, DEFAULT_APP_ID)
                ): str,
                vol.Required(
                    CONF_APP_SECRET,
                    default=current.get(CONF_APP_SECRET, DEFAULT_APP_SECRET),
                ): str,
                vol.Required(
                    CONF_APP_VERSION,
                    default=current.get(CONF_APP_VERSION, DEFAULT_APP_VERSION),
                ): str,
                vol.Optional(CONF_HOME_ID, default=current.get(CONF_HOME_ID, "")): str,
                vol.Optional(CONF_DEVICE_ID, default=current.get(CONF_DEVICE_ID, "")): str,
            }
        )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=reconfigure_schema,
            errors=errors,
        )


class EmaldoOptionsFlow(OptionsFlow):
    """Handle Emaldo options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage schedule polling options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCHEDULE_START_HOUR,
                        default=options.get(
                            CONF_SCHEDULE_START_HOUR, DEFAULT_SCHEDULE_START_HOUR
                        ),
                    ): vol.All(int, vol.Range(min=0, max=23)),
                    vol.Required(
                        CONF_SCHEDULE_START_MINUTE,
                        default=options.get(
                            CONF_SCHEDULE_START_MINUTE, DEFAULT_SCHEDULE_START_MINUTE
                        ),
                    ): vol.All(int, vol.Range(min=0, max=59)),
                    vol.Required(
                        CONF_SCHEDULE_INTERVAL,
                        default=options.get(
                            CONF_SCHEDULE_INTERVAL, DEFAULT_SCHEDULE_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=600, max=86400)),
                    vol.Required(
                        CONF_REALTIME_STREAM_MODE,
                        default=options.get(
                            CONF_REALTIME_STREAM_MODE, REALTIME_STREAM_MODE
                        ),
                    ): bool,
                }
            ),
        )
