"""The google_maps component."""
from __future__ import annotations

from asyncio import Lock
from collections.abc import Callable, Mapping
from dataclasses import asdict as dc_asdict, dataclass, field
from datetime import datetime, timedelta
from functools import partial
import logging
from pathlib import Path
from typing import Any, NewType, Self, cast

from homeassistant.components.device_tracker import DOMAIN as DT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    ATTR_ENTITY_PICTURE,
    ATTR_GPS_ACCURACY,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util, slugify

from .const import (
    ATTR_ADDRESS,
    ATTR_LAST_SEEN,
    ATTR_NICKNAME,
    CONF_COOKIES_FILE,
    CONF_CREATE_ACCT_ENTITY,
    COOKIE_WARNING_PERIOD,
    CREDENTIALS_FILE,
    DOMAIN,
    NAME_PREFIX,
)
from .gm_loc_sharing import (
    GMLocSharing,
    GMPerson,
    InvalidCookies,
    InvalidCookiesFile,
    InvalidData,
    RequestFailed,
)

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.DEVICE_TRACKER]


def old_cookies_file_path(hass: HomeAssistant, username: str) -> Path:
    """Return path to cookies file from legacy implementation."""
    return Path(hass.config.path()) / f"{CREDENTIALS_FILE}.{slugify(username)}"


def cookies_file_path(hass: HomeAssistant, cookies_file: str) -> Path:
    """Return path to cookies file."""
    return Path(hass.config.path()) / STORAGE_DIR / DOMAIN / cookies_file


def exp_2_str(expiration: datetime | None) -> str:
    """Convert expiration to a string."""
    return str(expiration) if expiration is not None else "unknown"


def expiring_soon(expiration: datetime | None) -> bool:
    """Return if cookies are expiring soon."""
    return expiration is not None and expiration - dt_util.now() < COOKIE_WARNING_PERIOD


class FromAttributesError(Exception):
    """Cannot create object from state attributes."""


@dataclass(frozen=True)
class LocationData:
    """Location data."""

    address: str
    gps_accuracy: int
    last_seen: datetime
    latitude: float
    longitude: float

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the data."""
        return dc_asdict(self)

    @staticmethod
    def _last_seen(data: Mapping[str, Any], key: str) -> datetime:
        """Get last_seen from mapping, converting to datetime if necessary."""
        last_seen: datetime | str | None
        try:
            last_seen = cast(datetime | str, data[key])
            if isinstance(last_seen, datetime):
                return last_seen
            last_seen = dt_util.parse_datetime(last_seen)
        except (KeyError, TypeError) as err:
            raise FromAttributesError from err
        if last_seen is None:
            raise FromAttributesError
        return last_seen

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        """Initialize location data from a dict."""
        try:
            last_seen = cls._last_seen(restored, "last_seen")
        except FromAttributesError:
            return None
        try:
            return cls(
                restored["address"],
                restored["gps_accuracy"],
                last_seen,
                restored["latitude"],
                restored["longitude"],
            )
        except KeyError:
            return None

    @classmethod
    def from_person(cls, person: GMPerson) -> Self:
        """Initialize location data from GMPerson object."""
        return cls(
            person.address,
            person.gps_accuracy,
            person.last_seen,
            person.latitude,
            person.longitude,
        )

    @classmethod
    def from_attributes(cls, attrs: Mapping[str, Any]) -> Self:
        """Initialize location data from state attributes."""
        last_seen = cls._last_seen(attrs, ATTR_LAST_SEEN)
        try:
            return cls(
                attrs[ATTR_ADDRESS],
                attrs[ATTR_GPS_ACCURACY],
                last_seen,
                attrs[ATTR_LATITUDE],
                attrs[ATTR_LONGITUDE],
            )
        except KeyError as err:
            raise FromAttributesError from err


@dataclass(frozen=True)
class MiscData:
    """Miscellaneous data."""

    battery_charging: bool | None
    battery_level: int | None
    entity_picture: str | None
    full_name: str
    nickname: str

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the data."""
        return dc_asdict(self)

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        """Initialize miscellaneous data from a dict."""
        try:
            return cls(
                restored["battery_charging"],
                restored["battery_level"],
                restored["entity_picture"],
                restored["full_name"],
                restored["nickname"],
            )
        except KeyError:
            return None

    @classmethod
    def from_person(cls, person: GMPerson) -> Self:
        """Initialize miscellaneous data from GMPerson object."""
        return cls(
            person.battery_charging,
            person.battery_level,
            person.picture_url,
            person.full_name,
            person.nickname,
        )

    @classmethod
    def from_attributes(cls, attrs: Mapping[str, Any], full_name: str) -> Self:
        """Initialize miscellaneous data from state attributes."""
        try:
            return cls(
                attrs.get(ATTR_BATTERY_CHARGING),
                attrs.get(ATTR_BATTERY_LEVEL),
                attrs.get(ATTR_ENTITY_PICTURE),
                full_name,
                attrs[ATTR_NICKNAME],
            )
        except KeyError as err:
            raise FromAttributesError from err


@dataclass(frozen=True)
class PersonData(ExtraStoredData):
    """Shared person data."""

    loc: LocationData | None
    misc: MiscData | None

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the data."""
        return dc_asdict(self)

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        """Return PersonData created from a dict."""
        if (loc := restored.get("loc")) is not None:
            loc = LocationData.from_dict(loc)
        if (misc := restored.get("misc")) is not None:
            misc = MiscData.from_dict(misc)
        return cls(loc, misc)

    @classmethod
    def from_person(cls, person: GMPerson) -> Self:
        """Initialize shared person data from GMPerson object."""
        return cls(
            LocationData.from_person(person),
            MiscData.from_person(person),
        )


ConfigID = NewType("ConfigID", str)
UniqueID = NewType("UniqueID", str)
GMData = dict[UniqueID, PersonData]
GMDataUpdateCoordinator = DataUpdateCoordinator[GMData]


class ConfigUniqueIDs:
    """Unique ID config assignments.

    Since multiple Google accounts might be be added, and it's possible for people to
    have shared their location with more than one of those accounts, to avoid having the
    same Entity being created by more than one account (i.e., ConfigEntry), keep a
    record of which config each entity is, or will be, associated with. This will not
    only avoid having to keep querying the Entity Registry, it will also avoid race
    conditions where multiple configs might try to create an Entity for the same shared
    person at the same time.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize assignments from Entity Registry."""
        self._all_uids: set[UniqueID] = set()
        self._cfg_uids: dict[ConfigID, set[UniqueID]] = {}

        ent_reg = er.async_get(hass)
        for cfg in hass.config_entries.async_entries(DOMAIN):
            cid = cast(ConfigID, cfg.entry_id)
            cfg_uids = {
                cast(UniqueID, ent.unique_id)
                for ent in er.async_entries_for_config_entry(ent_reg, cid)
            }
            self._all_uids.update(cfg_uids)
            self._cfg_uids[cid] = cfg_uids

    @property
    def empty(self) -> bool:
        """Return if no unique IDs are assigned to any config."""
        if not self._all_uids:
            assert not self._cfg_uids
            return True
        return False

    def own(self, cid: ConfigID, uid: UniqueID) -> bool:
        """Return if config already owns unique ID."""
        return uid in self.owned(cid)

    def owned(self, cid: ConfigID) -> frozenset[UniqueID]:
        """Return unique IDs owned by config."""
        return frozenset(self._cfg_uids.get(cid, set()))

    def owned_by_others(self, cid: ConfigID) -> set[UniqueID]:
        """Return unique IDs that are owned by other configs."""
        return self._all_uids - self.owned(cid)

    def take(self, cid: ConfigID, uids: set[UniqueID]) -> set[UniqueID]:
        """Take ownership of a set of unique IDs.

        Returns set of unique IDs actually taken;
        i.e., that did not already belong to other configs.
        """
        uids = uids - self.owned_by_others(cid)
        self._all_uids.update(uids)
        self._cfg_uids.setdefault(cid, set()).update(uids)
        return uids

    def release(self, cid: ConfigID, uid: UniqueID) -> None:
        """Release ownership of a single unique ID if not owned by another config."""
        if uid in self.owned_by_others(cid):
            return
        self._all_uids.discard(uid)
        self._cfg_uids[cid].discard(uid)

    def remove(self, cid: ConfigID) -> None:
        """Remove config, releasing any unique IDs it owned."""
        self._all_uids.difference_update(self._cfg_uids.pop(cid, set()))


@dataclass
class GMIntegData:
    """Google Maps integration data."""

    unique_ids: ConfigUniqueIDs
    cookie_locks: dict[ConfigID, Lock] = field(default_factory=dict)
    coordinators: dict[ConfigID, GMDataUpdateCoordinator] = field(default_factory=dict)


async def entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up config entry."""
    cid = ConfigID(entry.entry_id)
    username = entry.data[CONF_USERNAME]
    cf_path = cookies_file_path(hass, entry.data[CONF_COOKIES_FILE])
    create_acct_entity = entry.options[CONF_CREATE_ACCT_ENTITY]
    scan_interval = entry.options[CONF_SCAN_INTERVAL]

    if not (gmi_data := cast(GMIntegData | None, hass.data.get(DOMAIN))):
        hass.data[DOMAIN] = gmi_data = GMIntegData(ConfigUniqueIDs(hass))

    @callback
    def unpublish_cookie_lock() -> None:
        """Remove cookie lock from hass.data."""
        del gmi_data.cookie_locks[cid]

    # "Publish" cookie lock now on the odd chance that the options flow for this config
    # entry happens to also start while we're loading cookies, etc. below.
    gmi_data.cookie_locks[cid] = cookie_lock = Lock()
    entry.async_on_unload(unpublish_cookie_lock)

    @callback
    def create_issue(_now: datetime | None = None) -> None:
        """Create repair issue for cookies which are expiring soon."""
        async_create_issue(
            hass,
            DOMAIN,
            cid,
            is_fixable=False,
            is_persistent=False,
            severity=IssueSeverity.WARNING,
            translation_key="expiring_soon",
            translation_placeholders={"entry_id": cid, "username": username},
        )

    unsub_expiration: Callable[[], None] | None = None

    @callback
    def remove_expiration_listener() -> None:
        """Remove expiration listener."""
        nonlocal unsub_expiration

        if unsub_expiration:
            unsub_expiration()
            unsub_expiration = None

    entry.async_on_unload(remove_expiration_listener)
    cookies_last_saved: datetime

    def cookies_file_synced(final_write: bool = False) -> None:
        """Cookies file synced with current cookies."""
        nonlocal cookies_last_saved, unsub_expiration

        cookies_expiration = api.cookies_expiration
        cookies_last_saved = dt_util.now()
        if expiring_soon(cookies_expiration):
            create_issue()
        else:
            async_delete_issue(hass, DOMAIN, cid)
            if cookies_expiration and not final_write:
                remove_expiration_listener()
                unsub_expiration = async_track_point_in_time(
                    hass, create_issue, cookies_expiration - COOKIE_WARNING_PERIOD
                )

    api = GMLocSharing(username)
    async with cookie_lock:
        try:
            await hass.async_add_executor_job(api.load_cookies, str(cf_path))
        except (InvalidCookiesFile, InvalidCookies) as err:
            raise ConfigEntryAuthFailed(f"{err.__class__.__name__}: {err}") from err
        cookies_file_synced()

    async def save_cookies_if_changed(event: Event | None = None) -> None:
        """Save session's cookies if changed."""
        final_write = bool(event)
        async with cookie_lock:
            if not (
                api.cookies_changed
                and (
                    final_write
                    or dt_util.now() - cookies_last_saved  # noqa: F821
                    >= timedelta(minutes=15)
                )
            ):
                return
            try:
                await hass.async_add_executor_job(api.save_cookies, str(cf_path))
            except OSError as err:
                _LOGGER.error(
                    "Error while saving cookies: %s: %s", err.__class__.__name__, err
                )
            cookies_file_synced(final_write)

    # For "account person", unique ID is username (which is also returned in person.id.)
    ent_reg = er.async_get(hass)
    unique_ids = gmi_data.unique_ids
    if create_acct_entity:
        if not unique_ids.own(cid, username) and unique_ids.take(cid, {username}):
            ent_reg.async_get_or_create(
                DT_DOMAIN,
                DOMAIN,
                username,
                config_entry=entry,
                original_name=f"{NAME_PREFIX} {username}",
            )
    elif unique_ids.own(cid, username):
        if entity_id := ent_reg.async_get_entity_id(DT_DOMAIN, DOMAIN, username):
            ent_reg.async_remove(entity_id)
        dev_reg = dr.async_get(hass)
        if device := dev_reg.async_get_device({(DOMAIN, username)}):
            dev_reg.async_remove_device(device.id)
        unique_ids.release(cid, username)

    async def update_method() -> GMData:
        """Get shared location data."""
        async with cookie_lock:
            try:
                await hass.async_add_executor_job(api.get_new_data)
                people = api.get_people(create_acct_entity)
            except InvalidCookies as err:
                raise ConfigEntryAuthFailed(f"{err.__class__.__name__}: {err}") from err
            except (RequestFailed, InvalidData) as err:
                raise UpdateFailed(f"{err.__class__.__name__}: {err}") from err

        await hass.async_create_task(save_cookies_if_changed())
        return {
            UniqueID(person.id): PersonData.from_person(person) for person in people
        }

    coordinator = GMDataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"Google Maps ({entry.title})",
        update_interval=timedelta(seconds=scan_interval),
        update_method=update_method,
    )
    await coordinator.async_config_entry_first_refresh()
    gmi_data.coordinators[cid] = coordinator

    entry.async_on_unload(entry.add_update_listener(entry_updated))
    entry.async_on_unload(
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_FINAL_WRITE, save_cookies_if_changed
        )
    )
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    if unload_ok:
        # TODO: Save cookies & close session???
        gmi_data = cast(GMIntegData, hass.data[DOMAIN])
        cid = ConfigID(entry.entry_id)
        del gmi_data.coordinators[cid]
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry."""
    gmi_data = cast(GMIntegData, hass.data[DOMAIN])
    gmi_data.unique_ids.remove(ConfigID(entry.entry_id))
    hass.async_add_executor_job(
        partial(
            cookies_file_path(hass, entry.data[CONF_COOKIES_FILE]).unlink,
            missing_ok=True,
        )
    )
