"""Config flow for Google Maps."""
from __future__ import annotations

from abc import abstractmethod
from asyncio import Lock
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
import logging
from os import PathLike
from pathlib import Path
from typing import Any, cast

from propcache.api import cached_property
import voluptuous as vol

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryBaseFlow,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
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
from homeassistant.loader import async_get_integration
from homeassistant.util.uuid import random_uuid_hex

from .const import (
    CONF_COOKIES_FILE,
    CONF_CREATE_ACCT_ENTITY,
    CONF_MAX_GPS_ACCURACY,
    DEF_SCAN_INTERVAL_SEC,
    DOMAIN,
)
from .cookies import CHROME_PROCEDURE, EDGE_PROCEDURE, FIREFOX_PROCEDURE
from .coordinator import GMConfigEntry
from .gm_loc_sharing import (
    GMLocSharing,
    InvalidCookies,
    InvalidCookiesFile,
    InvalidData,
    RequestFailed,
)
from .helpers import cookies_file_path, exp_2_str, expiring_soon, old_cookies_file_path

_LOGGER = logging.getLogger(__name__)
_CONF_UPDATE_COOKIES = "update_cookies"
_CONF_USE_EXISTING_COOKIES = "use_existing_cookies"
_GMSERVICE_ERRORS = (InvalidCookies, InvalidCookiesFile, InvalidData, RequestFailed)


class GoogleMapsFlow(ConfigEntryBaseFlow):
    """Google Maps flow mixin."""

    _username: str
    _options: dict[str, Any]

    def _init_gmflow(self, username: str, options: Mapping[str, Any]) -> None:
        """Initialize GoogleMapsFlow."""
        self._username = username
        self._options = deepcopy(dict(options))

    async def async_step_account_entity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Determine if entity should be created for account."""
        if user_input is not None:
            self._options[CONF_CREATE_ACCT_ENTITY] = user_input[CONF_CREATE_ACCT_ENTITY]
            return await self.async_step_max_gps_accuracy()

        data_schema = vol.Schema(
            {vol.Required(CONF_CREATE_ACCT_ENTITY): BooleanSelector()}
        )
        data_schema = self.add_suggested_values_to_schema(
            data_schema,
            {CONF_CREATE_ACCT_ENTITY: self._options.get(CONF_CREATE_ACCT_ENTITY, True)},
        )
        if doc := (await async_get_integration(self.hass, DOMAIN)).documentation:
            doc = (
                "[Missing Data for Account Tracker]"
                f"({doc}#missing-data-for-account-tracker)"
            )
        else:
            doc = "the integration's documentation"
        return self.async_show_form(
            step_id="account_entity",
            data_schema=data_schema,
            description_placeholders={"doc": doc, "username": self._username},
            last_step=False,
        )

    async def async_step_max_gps_accuracy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Get maximum GPS accuracy."""
        if user_input is not None:
            self._options[CONF_MAX_GPS_ACCURACY] = int(
                user_input[CONF_MAX_GPS_ACCURACY]
            )
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
            {CONF_MAX_GPS_ACCURACY: self._options.get(CONF_MAX_GPS_ACCURACY, 1000)},
        )
        return self.async_show_form(
            step_id="max_gps_accuracy", data_schema=data_schema, last_step=False
        )

    async def async_step_update_period(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Get update period."""
        if user_input is not None:
            self._options[CONF_SCAN_INTERVAL] = int(
                cv.time_period_dict(user_input[CONF_SCAN_INTERVAL]).total_seconds()
            )
            return await self.async_step_done()

        data_schema = vol.Schema({vol.Required(CONF_SCAN_INTERVAL): DurationSelector()})
        default = self._options.get(CONF_SCAN_INTERVAL, DEF_SCAN_INTERVAL_SEC)
        def_m, def_s = divmod(default, 60)
        def_h, def_m = divmod(def_m, 60)
        data_schema = self.add_suggested_values_to_schema(
            data_schema,
            {CONF_SCAN_INTERVAL: {"hours": def_h, "minutes": def_m, "seconds": def_s}},
        )
        return self.async_show_form(step_id="update_period", data_schema=data_schema)

    @abstractmethod
    async def async_step_done(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish the user config or options flow."""


class GoogleMapsConfigFlow(ConfigFlow, GoogleMapsFlow, domain=DOMAIN):
    """Google Maps config flow."""

    VERSION = 3

    _get_new_cookies = True
    _expiration: datetime | None
    _api: GMLocSharing
    _data: dict[str, Any]

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GoogleMapsOptionsFlow:
        """Get the options flow for this handler."""
        return GoogleMapsOptionsFlow()

    @cached_property
    def _is_existing(self) -> bool:
        """Return if this is a config flow for an existing config entry."""
        return self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE)

    # https://developers.home-assistant.io/blog/2024/10/21/reauth-reconfigure-helpers/
    # says _get_reauth_entry & _get_reconfigure_entry should be called in each step it
    # is needed, and specifically that the result should NOT be "cached" in a class
    # instance variable.
    @property
    def _update_entry(self) -> GMConfigEntry | None:
        """Return config entry associated with flow (i.e., for reauth & reconfigure)."""
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry()
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return None

    def _init_gmcfgflow(
        self, username: str, options: Mapping[str, Any], data: Mapping[str, Any]
    ) -> None:
        """Initialize GoogleMapsConfigFlow."""
        self._init_gmflow(username, options)
        self._api = GMLocSharing(username)
        self._data = deepcopy(dict(data))

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start user config flow."""
        return await self.async_step_username()

    async def async_step_reauth(self, data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthorization flow."""
        return await self.async_step_username()

    async def async_step_reconfigure(self, data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reconfiguration flow."""
        return await self.async_step_username()

    async def async_step_username(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Get username."""
        errors = {}
        username: str | None

        # Keep local copy for duration of step.
        update_entry = self._update_entry
        if user_input is not None:
            username = cast(str, user_input[CONF_USERNAME])
            # Set unique ID to username and see if any existing config entries are
            # already using that username.
            entry_w_username = await self.async_set_unique_id(username)
            # If entry is being updated (reauth or reconfig), then the username must not
            # be in use by another config entry, or if it is, it must be the entry being
            # updated. To put that another way, if the username is being changed, then
            # it can't be already in use by another config, or if it's not being
            # being changed, then the entry using it must be the one being updated.
            if update_entry:
                if not entry_w_username or entry_w_username is update_entry:
                    self._init_gmcfgflow(
                        username, update_entry.options, update_entry.data
                    )
                    return await self.async_step_check_cookies()
            # If creating a new entry, then no other existing entry can be using the
            # same username.
            elif not entry_w_username:
                self._init_gmcfgflow(username, {}, {})
                return await self.async_step_get_cookies()
            errors[CONF_USERNAME] = "already_configured"
        elif update_entry:
            username = update_entry.unique_id
            assert username
        else:
            username = None

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.EMAIL)
                )
            }
        )
        if username:
            data_schema = self.add_suggested_values_to_schema(
                data_schema, {CONF_USERNAME: username}
            )
        return self.async_show_form(
            step_id="username", data_schema=data_schema, errors=errors, last_step=False
        )

    async def async_step_check_cookies(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Get username."""

        if user_input is not None:
            if user_input[_CONF_UPDATE_COOKIES]:
                return await self.async_step_get_cookies()
            self._get_new_cookies = False
            return await self.async_step_done()

        # Keep local copy for duration of step.
        update_entry = self._update_entry
        assert update_entry
        file_ok = await self._existing_cookies_file_ok(update_entry)
        data_schema = vol.Schema(
            {vol.Required(_CONF_UPDATE_COOKIES): BooleanSelector()}
        )
        data_schema = self.add_suggested_values_to_schema(
            data_schema,
            {_CONF_UPDATE_COOKIES: not file_ok or expiring_soon(self._expiration)},
        )
        return self.async_show_form(
            step_id="check_cookies",
            data_schema=data_schema,
            description_placeholders={
                "username": self._username,
                "expiration": exp_2_str(self._expiration),
            },
            last_step=False,
        )

    async def async_step_get_cookies(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Get a cookies file."""
        if user_input is not None:
            if not user_input[_CONF_USE_EXISTING_COOKIES]:
                return await self.async_step_get_cookies_procedure_menu()
            if self._is_existing:
                return await self.async_step_done()
            return await self.async_step_account_entity()

        cf_path = old_cookies_file_path(self.hass, self._username)
        if not await self.hass.async_add_executor_job(cf_path.is_file):
            return await self.async_step_get_cookies_procedure_menu()
        if not await self.hass.async_add_executor_job(self._cookies_file_ok, cf_path):
            return await self.async_step_old_cookies_invalid(cf_path=cf_path)

        data_schema = vol.Schema(
            {vol.Required(_CONF_USE_EXISTING_COOKIES): BooleanSelector()}
        )
        data_schema = self.add_suggested_values_to_schema(
            data_schema, {_CONF_USE_EXISTING_COOKIES: True}
        )
        return self.async_show_form(
            step_id="get_cookies",
            data_schema=data_schema,
            description_placeholders={
                "username": self._username,
                "cookies_file": str(cf_path.name),
                "expiration": exp_2_str(self._expiration),
            },
            last_step=False,
        )

    async def async_step_old_cookies_invalid(
        self, user_input: dict[str, Any] | None = None, cf_path: Path | None = None
    ) -> ConfigFlowResult:
        """Upload a cookies file."""
        if user_input is not None:
            return await self.async_step_get_cookies_procedure_menu()

        assert cf_path
        return self.async_show_form(
            step_id="old_cookies_invalid",
            description_placeholders={
                "username": self._username,
                "cookies_file": str(cf_path.name),
            },
            last_step=False,
        )

    async def async_step_get_cookies_procedure_menu(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display a list of procedures for obtaining a cookies file."""
        return self.async_show_menu(
            step_id="get_cookies_procedure_menu",
            menu_options=["chrome", "edge", "firefox", "cookies_upload"],
        )

    async def async_step_chrome(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display detailed instructions for Google Chrome."""
        if user_input is not None:
            return await self.async_step_cookies_upload()

        return self.async_show_form(
            step_id="chrome",
            description_placeholders={"procedure": CHROME_PROCEDURE},
            last_step=False,
        )

    async def async_step_edge(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display detailed instructions for Microsoft Edge."""
        if user_input is not None:
            return await self.async_step_cookies_upload()

        return self.async_show_form(
            step_id="edge",
            description_placeholders={"procedure": EDGE_PROCEDURE},
            last_step=False,
        )

    async def async_step_firefox(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Display detailed instructions for Mozilla Firefox."""
        if user_input is not None:
            return await self.async_step_cookies_upload()

        return self.async_show_form(
            step_id="firefox",
            description_placeholders={"procedure": FIREFOX_PROCEDURE},
            last_step=False,
        )

    async def async_step_cookies_upload(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Upload a cookies file."""
        errors = {}

        if user_input is not None:
            if await self.hass.async_add_executor_job(
                self._uploaded_cookies_ok, user_input[CONF_COOKIES_FILE]
            ):
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
    ) -> ConfigFlowResult:
        """Use uploaded cookie file or try again."""
        menu_options = [
            "done" if self._is_existing else "account_entity",
            "get_cookies_procedure_menu",
        ]
        return self.async_show_menu(
            step_id="uploaded_cookie_menu",
            menu_options=menu_options,
            description_placeholders={
                "username": self._username,
                "expiration": exp_2_str(self._expiration),
            },
        )

    async def async_step_done(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish the config flow."""
        # Save cookies.
        if self._get_new_cookies:
            await self._save_new_cookies()

        # Keep local copy for duration of step.
        update_entry = self._update_entry
        if update_entry:
            assert self.unique_id == self._username
            # TODO: Use async_update_reload_and_abort(..., reload_even_if_entry_is_unchanged=False)???
            self.hass.config_entries.async_update_entry(
                update_entry,
                data=self._data,
                options=self._options,
                title=self._username,
                unique_id=self.unique_id,
            )
            if self.source == SOURCE_REAUTH:
                action = "Re-authentication"
            else:
                action = "Re-configuration"
            _LOGGER.debug("%s was successful", action)
            return self.async_abort(reason=f"{self.source}_successful")

        return self.async_create_entry(
            title=self._username, data=self._data, options=self._options
        )

    async def _existing_cookies_file_ok(self, entry: GMConfigEntry) -> bool:
        """Return if existing cookies file is ok."""
        if hasattr(entry, "runtime_data"):
            lock = entry.runtime_data.coordinator.cookie_lock
        else:
            lock = Lock()
        async with lock:
            return await self.hass.async_add_executor_job(
                self._cookies_file_ok,
                cookies_file_path(self.hass, entry.data[CONF_COOKIES_FILE]),
            )

    async def _save_new_cookies(self) -> None:
        """Save new cookies to newly named file."""
        self._data[CONF_COOKIES_FILE] = cookies_file = random_uuid_hex()
        await self.hass.async_add_executor_job(self._save_cookies, cookies_file)

    def _cookies_file_ok(self, cookies_file: str | PathLike) -> bool:
        """Determine if cookies in file are ok.

        Must be called in an executor.
        """
        try:
            self._api.load_cookies(str(cookies_file))
            self._expiration = self._api.cookies_expiration
            self._api.get_new_data()
        except _GMSERVICE_ERRORS as err:
            _LOGGER.debug(
                "Error while validating cookies file %s: %r", cookies_file, err
            )
            return False
        return True

    def _uploaded_cookies_ok(self, uploaded_file_id: str) -> bool:
        """Determine if cookies in uploaded cookies file are ok.

        Must be called in an executor.
        """
        with process_uploaded_file(self.hass, uploaded_file_id) as cf_path:
            return self._cookies_file_ok(cf_path)

    def _save_cookies(self, cookies_file: str) -> None:
        """Save cookies.

        Must be called in an executor.
        """
        cf_path = cookies_file_path(self.hass, cookies_file)
        cf_path.parent.mkdir(exist_ok=True)
        self._api.save_cookies(str(cf_path))


class GoogleMapsOptionsFlow(OptionsFlow, GoogleMapsFlow):
    """Google Maps options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start options flow."""
        username = self.config_entry.unique_id
        assert username
        self._init_gmflow(username, self.config_entry.options)
        return await self.async_step_account_entity()

    async def async_step_done(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish the flow."""
        return self.async_create_entry(title="", data=self._options)
