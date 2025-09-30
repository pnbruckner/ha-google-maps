"""DataUpdateCoordinator for the Google Maps integration."""
from __future__ import annotations

from asyncio import Lock
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_FINAL_WRITE,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_COOKIES_FILE,
    CONF_CREATE_ACCT_ENTITY,
    COOKIE_WARNING_PERIOD,
    DOMAIN,
)
from .gm_loc_sharing import (
    GMLocSharing,
    InvalidCookies,
    InvalidCookiesFile,
    InvalidData,
    RequestFailed,
)
from .helpers import ConfigID, PersonData, UniqueID, cookies_file_path, expiring_soon

_LOGGER = logging.getLogger(__name__)


GMData = dict[UniqueID, PersonData]


class GMDataUpdateCoordinator(DataUpdateCoordinator[GMData]):
    """Google Maps data update coordinator."""

    config_entry: ConfigEntry
    _cookies_last_synced: datetime
    _unsub_exp: Callable[[], None] | None = None

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        self._cid = ConfigID(entry.entry_id)
        self._username = entry.data[CONF_USERNAME]
        self._cookies_file = str(
            cookies_file_path(hass, entry.options[CONF_COOKIES_FILE])
        )
        self._create_acct_entity = entry.options[CONF_CREATE_ACCT_ENTITY]

        self._api = GMLocSharing(self._username)
        self.cookie_lock = Lock()
        self._unsub_final_write = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_FINAL_WRITE, self._save_cookies_if_changed
        )

        scan_interval = timedelta(seconds=entry.options[CONF_SCAN_INTERVAL])
        super().__init__(hass, _LOGGER, name=entry.title, update_interval=scan_interval)
        # always_update added in 2023.9.0b0.
        if hasattr(self, "always_update"):
            self.always_update = False

    async def async_shutdown(self) -> None:
        """Cancel listeners, save cookies & close API."""
        await super().async_shutdown()
        self._unsub_all()
        cur_cookies_file = str(
            cookies_file_path(self.hass, self.config_entry.options[CONF_COOKIES_FILE])
        )
        # Has cookies file name changed, e.g., due to reauth or user reconfiguration?
        # If not, save cookies to existing file. If so, delete file that was being used
        # because there's a new one to be used after reload/restart.
        if cur_cookies_file == self._cookies_file:
            await self._save_cookies_if_changed(shutting_down=True)
        else:
            await self.hass.async_add_executor_job(Path(self._cookies_file).unlink)
        self._api.close()

    def _unsub_all(self) -> None:
        """Run removers."""
        self._unsub_final_write()
        self._unsub_expiration()

    def _unsub_expiration(self) -> None:
        """Remove expiration listener."""
        if self._unsub_exp:
            self._unsub_exp()
            self._unsub_exp = None

    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        # Load the cookies before first update.
        async with self.cookie_lock:
            try:
                await self.hass.async_add_executor_job(
                    self._api.load_cookies, self._cookies_file
                )
            except (InvalidCookiesFile, InvalidCookies) as err:
                raise ConfigEntryAuthFailed(f"{err.__class__.__name__}: {err}") from err
            self._cookies_file_synced()

    async def _async_update_data(self) -> GMData:
        """Fetch the latest data from the source."""
        async with self.cookie_lock:
            try:
                await self.hass.async_add_executor_job(self._api.get_new_data)
                people = self._api.get_people(self._create_acct_entity)
            except InvalidCookies as err:
                raise ConfigEntryAuthFailed(f"{err.__class__.__name__}: {err}") from err
            except (RequestFailed, InvalidData) as err:
                raise UpdateFailed(f"{err.__class__.__name__}: {err}") from err

        await self.hass.async_create_task(self._save_cookies_if_changed())
        return {
            UniqueID(person.id): PersonData.from_person(person) for person in people
        }

    async def _save_cookies_if_changed(
        self,
        event: Event | None = None,
        shutting_down: bool = False,
    ) -> None:
        """Save session's cookies if changed."""
        shutting_down |= bool(event)
        async with self.cookie_lock:
            if not (
                self._api.cookies_changed
                and (
                    shutting_down
                    or dt_util.utcnow() - self._cookies_last_synced  # noqa: F821
                    >= timedelta(minutes=15)
                )
            ):
                return
            try:
                await self.hass.async_add_executor_job(
                    self._api.save_cookies, self._cookies_file
                )
            except OSError as err:
                self.logger.error(
                    "Error while saving cookies: %s: %s", err.__class__.__name__, err
                )
            self._cookies_file_synced(shutting_down)

    def _cookies_file_synced(self, shutting_down: bool = False) -> None:
        """Cookies file synced with current cookies."""
        cookies_expiration = self._api.cookies_expiration
        self._cookies_last_synced = dt_util.utcnow()
        if expiring_soon(cookies_expiration):
            self._create_issue()
        else:
            async_delete_issue(self.hass, DOMAIN, self._cid)
            if cookies_expiration and not shutting_down:
                self._unsub_expiration()
                self._unsub_exp = async_track_point_in_utc_time(
                    self.hass,
                    self._create_issue,
                    cookies_expiration - COOKIE_WARNING_PERIOD,
                )

    @callback
    def _create_issue(self, _utcnow: datetime | None = None) -> None:
        """Create repair issue for cookies which are expiring soon."""
        async_create_issue(
            self.hass,
            DOMAIN,
            self._cid,
            is_fixable=False,
            is_persistent=False,
            severity=IssueSeverity.WARNING,
            translation_key="expiring_soon",
            translation_placeholders={
                "entry_id": self._cid,
                "username": self._username,
            },
        )


type GMConfigEntry = ConfigEntry[GMDataUpdateCoordinator]
