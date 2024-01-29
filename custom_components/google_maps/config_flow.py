"""Config flow for Google Maps."""
from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
import logging
from os import PathLike
from pathlib import Path
from typing import Any

from locationsharinglib import Service
from locationsharinglib.locationsharinglibexceptions import (
    InvalidCookieFile,
    InvalidCookies,
    InvalidData,
)
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

from . import (
    cookies_file_path,
    exp_2_str,
    expiring_soon,
    get_expiration,
    old_cookies_file_path,
)
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
    # The following are only used in the reauth flow.
    _reauth_entry: ConfigEntry | None = None
    _cookies_file: str

    @property
    @abstractmethod
    def options(self) -> dict[str, Any]:
        """Return mutable copy of options."""

    def _cookies_file_ok(self, cookies_file: str | PathLike) -> bool:
        """Determine if cookies in file are ok.

        Must be called in an executor.
        """
        try:
            Service(cookies_file, self._username)
        except (InvalidCookieFile, InvalidCookies, InvalidData) as exc:
            _LOGGER.debug(
                "Error while validating cookies file %s: %r", cookies_file, exc
            )
            return False
        return True

    def _get_uploaded_cookies(self, uploaded_file_id: str) -> str | None:
        """Validate and read cookies from uploaded cookies file.

        Must be called in an executor.
        """
        with process_uploaded_file(self.hass, uploaded_file_id) as cf_path:
            if self._cookies_file_ok(cf_path):
                return cf_path.read_text()
            return None

    def _save_cookies(self, cookies_file: str) -> None:
        """Save cookies.

        Must be called in an executor.
        """
        cf_path = cookies_file_path(self.hass, cookies_file)
        cf_path.parent.mkdir(exist_ok=True)
        cf_path.write_text(self._cookies)

    async def async_step_cookies(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Get a cookies file."""
        if user_input is not None:
            if not user_input[_CONF_USE_EXISTING_COOKIES]:
                del self._cookies
                return await self.async_step_cookies_upload()
            if self._reauth_entry:
                return await self.async_step_reauth_done()
            return await self.async_step_account_entity()

        cf_path = old_cookies_file_path(self.hass, self._username)
        if not cf_path.is_file():
            return await self.async_step_cookies_upload()
        if not await self.hass.async_add_executor_job(self._cookies_file_ok, cf_path):
            return await self.async_step_old_cookies_invalid(cf_path=cf_path)

        self._cookies = await self.hass.async_add_executor_job(cf_path.read_text)

        data_schema = vol.Schema(
            {vol.Required(_CONF_USE_EXISTING_COOKIES, default=True): BooleanSelector()}
        )
        return self.async_show_form(
            step_id="cookies",
            data_schema=data_schema,
            description_placeholders={
                "username": self._username,
                "cookies_file": str(cf_path.name),
                "expiration": exp_2_str(get_expiration(self._cookies)),
            },
            last_step=False,
        )

    async def async_step_old_cookies_invalid(
        self, user_input: dict[str, Any] | None = None, *, cf_path: Path | None = None
    ) -> FlowResult:
        """Upload a cookies file."""
        if user_input is not None:
            return await self.async_step_cookies_upload()

        assert cf_path
        return self.async_show_form(
            step_id="old_cookies_invalid",
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
            cookies = await self.hass.async_add_executor_job(
                self._get_uploaded_cookies, user_input[CONF_COOKIES_FILE]
            )
            if cookies:
                self._cookies = cookies
                return await self.async_step_uploaded_cookie_menu()
            errors[CONF_COOKIES_FILE] = "invalid_cookies_file"

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

    async def async_step_uploaded_cookie_menu(
        self, _: dict[str, Any] | None = None
    ) -> FlowResult:
        """Use uploaded cookie file or try again."""
        menu_options = [
            "reauth_done" if self._reauth_entry else "account_entity",
            "cookies_upload",
        ]
        return self.async_show_menu(
            step_id="uploaded_cookie_menu",
            menu_options=menu_options,
            description_placeholders={
                "username": self._username,
                "expiration": exp_2_str(get_expiration(self._cookies)),
            },
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
        """Finish the user config or options flow."""

    async def async_step_reauth_done(
        self, _: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finish the reauthorization flow."""
        raise NotImplementedError


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

    async def async_step_reauth(self, data: Mapping[str, Any]) -> FlowResult:
        """Start reauthorization flow."""
        _LOGGER.debug("async_step_reauth: %s", data)
        self._cookies_file = data[CONF_COOKIES_FILE]
        self._username = data[CONF_USERNAME]
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_cookies()

    async def async_step_done(self, _: dict[str, Any] | None = None) -> FlowResult:
        """Finish the user config flow."""
        # Save cookies.
        cookies_file = random_uuid_hex()
        await self.hass.async_add_executor_job(self._save_cookies, cookies_file)
        return self.async_create_entry(
            title=self._username,
            data={CONF_COOKIES_FILE: cookies_file, CONF_USERNAME: self._username},
            options=self.options,
        )

    async def async_step_reauth_done(
        self, _: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finish the reauthorization flow."""
        # Save cookies.
        assert self._reauth_entry
        await self.hass.async_add_executor_job(self._save_cookies, self._cookies_file)
        _LOGGER.debug("Reauthorization successful")
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
        )
        return self.async_abort(reason="reauth_successful")


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
        cf_path = cookies_file_path(
            self.hass, self.config_entry.data[CONF_COOKIES_FILE]
        )
        cookies = await self.hass.async_add_executor_job(cf_path.read_text)
        expiration = get_expiration(cookies)
        data_schema = vol.Schema(
            {vol.Required(_CONF_UPDATE_COOKIES): BooleanSelector()}
        )
        data_schema = self.add_suggested_values_to_schema(
            data_schema, {_CONF_UPDATE_COOKIES: expiring_soon(expiration)}
        )
        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            description_placeholders={
                "username": self._username,
                "expiration": exp_2_str(expiration),
            },
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
