"""Config flow for Google Maps."""
from __future__ import annotations

from abc import abstractmethod
import logging
from typing import Any

from locationsharinglib import Service
from locationsharinglib.locationsharinglibexceptions import InvalidCookies, InvalidData
import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowHandler, FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    BooleanSelector,
    DurationSelector,
    FileSelector,
    FileSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util.uuid import random_uuid_hex

from . import cookies_file_path, old_cookies_file_path
from .const import (
    CONF_COOKIES_FILE,
    CONF_CREATE_ACCT_ENTITY,
    CONF_MAX_GPS_ACCURACY,
    DEF_SCAN_INTERVAL_SEC,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
_CONF_UPDATE_COOKIES = "update_cookies"
_CONF_USE_EXISTING_COOKIES = "use_existing_cookies"


class GoogleMapsFlow(FlowHandler):
    """Google Maps flow mixin."""

    _username: str
    _cookies: str

    @property
    @abstractmethod
    def options(self) -> dict[str, Any]:
        """Return mutable copy of options."""

    def _get_uploaded_cookies(self, uploaded_file_id: str) -> str:
        """Validate and read cookies from uploaded cookies file."""
        with process_uploaded_file(self.hass, uploaded_file_id) as file_path:
            # Test cookies file.
            Service(file_path, self._username)
            return file_path.read_text()

    def _save_cookies(self, cookies_file: str) -> None:
        """Save cookies."""
        cf_path = cookies_file_path(self.hass, cookies_file)
        cf_path.parent.mkdir(exist_ok=True)
        cf_path.write_text(self._cookies)

    async def async_step_cookies(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Get a cookies file."""
        cf_path = old_cookies_file_path(self.hass, self._username)

        if user_input is not None:
            if not user_input[_CONF_USE_EXISTING_COOKIES]:
                return await self.async_step_cookies_upload()
            self._cookies = await self.hass.async_add_executor_job(cf_path.read_text)
            return await self.async_step_account_entity()

        if not cf_path.exists():
            return await self.async_step_cookies_upload()

        data_schema = vol.Schema(
            {vol.Required(_CONF_USE_EXISTING_COOKIES, default=True): BooleanSelector()}
        )
        return self.async_show_form(
            step_id="cookies",
            data_schema=data_schema,
            description_placeholders={
                "username": self._username,
                "cookies_file": str(cf_path.name),
            },
            last_step=False,
        )

    async def async_step_cookies_upload(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Upload a cookies file."""
        errors = {}

        if user_input is not None:
            try:
                self._cookies = await self.hass.async_add_executor_job(
                    self._get_uploaded_cookies, user_input[CONF_COOKIES_FILE]
                )
            except (InvalidCookies, InvalidData) as exc:
                _LOGGER.debug("Error while validating cookies file: %s", exc)
                errors[CONF_COOKIES_FILE] = "invalid_cookies_file"
            else:
                return await self.async_step_account_entity()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_COOKIES_FILE): FileSelector(
                    FileSelectorConfig(accept=".txt")
                )
            }
        )
        return self.async_show_form(
            step_id="cookies_upload",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={"username": self._username},
            last_step=False,
        )

    async def async_step_account_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Determine if entity should be created for account."""
        if user_input is not None:
            self.options[CONF_CREATE_ACCT_ENTITY] = user_input[CONF_CREATE_ACCT_ENTITY]
            return await self.async_step_max_gps_accuracy()

        data_schema = vol.Schema(
            {vol.Required(CONF_CREATE_ACCT_ENTITY): BooleanSelector()}
        )
        data_schema = self.add_suggested_values_to_schema(
            data_schema,
            {CONF_CREATE_ACCT_ENTITY: self.options.get(CONF_CREATE_ACCT_ENTITY, True)},
        )
        return self.async_show_form(
            step_id="account_entity",
            data_schema=data_schema,
            description_placeholders={
                "doc": "https://www.home-assistant.io/integrations/google_maps/"
            },
            last_step=False,
        )

    async def async_step_max_gps_accuracy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Get maximum GPS accuracy."""
        if user_input is not None:
            self.options[CONF_MAX_GPS_ACCURACY] = int(user_input[CONF_MAX_GPS_ACCURACY])
            return await self.async_step_update_period()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_MAX_GPS_ACCURACY): NumberSelector(
                    NumberSelectorConfig(min=0, mode=NumberSelectorMode.BOX)
                ),
            }
        )
        data_schema = self.add_suggested_values_to_schema(
            data_schema,
            {CONF_MAX_GPS_ACCURACY: self.options.get(CONF_MAX_GPS_ACCURACY, 1000)},
        )
        return self.async_show_form(
            step_id="max_gps_accuracy", data_schema=data_schema, last_step=False
        )

    async def async_step_update_period(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Get update period."""
        if user_input is not None:
            _LOGGER.debug("async_step_update_period: %s", user_input)
            self.options[CONF_SCAN_INTERVAL] = int(
                cv.time_period_dict(user_input[CONF_SCAN_INTERVAL]).total_seconds()
            )
            return await self.async_step_done()

        data_schema = vol.Schema({vol.Required(CONF_SCAN_INTERVAL): DurationSelector()})
        default = self.options.get(CONF_SCAN_INTERVAL, DEF_SCAN_INTERVAL_SEC)
        def_m, def_s = divmod(default, 60)
        def_h, def_m = divmod(def_m, 60)
        data_schema = self.add_suggested_values_to_schema(
            data_schema,
            {CONF_SCAN_INTERVAL: {"hours": def_h, "minutes": def_m, "seconds": def_s}},
        )
        return self.async_show_form(step_id="update_period", data_schema=data_schema)

    @abstractmethod
    async def async_step_done(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Finish the flow."""


class GoogleMapsConfigFlow(ConfigFlow, GoogleMapsFlow, domain=DOMAIN):
    """Google Maps config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize config flow."""
        self._options: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GoogleMapsOptionsFlow:
        """Get the options flow for this handler."""
        return GoogleMapsOptionsFlow(config_entry)
        # flow = GoogleMapsOptionsFlow(config_entry)
        # flow.init_step = "cookies"
        # return flow

    @property
    def options(self) -> dict[str, Any]:
        """Return mutable copy of options."""
        return self._options

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start user config flow."""
        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            return await self.async_step_cookies()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL)
                )
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=data_schema, last_step=False
        )

    async def async_step_done(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Finish the flow."""
        # Save cookies.
        cookies_file = random_uuid_hex()
        await self.hass.async_add_executor_job(self._save_cookies, cookies_file)
        return self.async_create_entry(
            title=self._username,
            data={CONF_COOKIES_FILE: cookies_file, CONF_USERNAME: self._username},
            options=self.options,
        )


class GoogleMapsOptionsFlow(OptionsFlowWithConfigEntry, GoogleMapsFlow):
    """Google Maps options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start options flow."""
        if user_input is not None:
            if user_input[_CONF_UPDATE_COOKIES]:
                return await self.async_step_cookies()
            return await self.async_step_account_entity()

        self._username = self.config_entry.data[CONF_USERNAME]
        data_schema = vol.Schema(
            {vol.Required(_CONF_UPDATE_COOKIES): BooleanSelector()}
        )
        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            description_placeholders={"username": self._username},
            last_step=False,
        )

    async def async_step_done(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Finish the flow."""
        if hasattr(self, "_cookies"):
            await self.hass.async_add_executor_job(
                self._save_cookies, self.config_entry.data[CONF_COOKIES_FILE]
            )
            # Cookies file content has been updated, so config entry needs to be
            # reloaded to use the new cookies. However, if none of the (other) options
            # have actually changed, the entry update listeners won't be called, and the
            # entry will therefore not get reloaded. If this is the case, initiate a
            # reload from here. We don't have to worry about the flow being completely
            # finished because neither the config data nor options are changing.
            if self.options == self.config_entry.options:
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self.config_entry.entry_id)
                )

        return self.async_create_entry(title="", data=self.options)
