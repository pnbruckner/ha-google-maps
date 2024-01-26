"""The google_maps component."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
import logging
from pathlib import Path
from typing import Any, NewType, Self, cast

from locationsharinglib import Person, Service
from locationsharinglib.locationsharinglibexceptions import (
    InvalidCookieFile,
    InvalidCookies,
    InvalidData,
)

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
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util, slugify

from .const import (
    ATTR_ADDRESS,
    ATTR_LAST_SEEN,
    ATTR_NICKNAME,
    CONF_COOKIES_FILE,
    CONF_CREATE_ACCT_ENTITY,
    CREDENTIALS_FILE,
    DOMAIN,
    NAME_PREFIX,
)

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.DEVICE_TRACKER]


def old_cookies_file_path(hass: HomeAssistant, username: str) -> Path:
    """Return path to cookies file from legacy implementation."""
    return Path(hass.config.path()) / f"{CREDENTIALS_FILE}.{slugify(username)}"


def cookies_file_path(hass: HomeAssistant, cookies_file: str) -> Path:
    """Return path to cookies file."""
    return Path(hass.config.path()) / STORAGE_DIR / DOMAIN / cookies_file


@dataclass(frozen=True)
class LocationData:
    """Location data."""

    address: str
    gps_accuracy: int
    last_seen: datetime
    latitude: float
    longitude: float

    @classmethod
    def from_person(cls, person: Person) -> Self:
        """Initialize location data from Person object."""
        return cls(
            person.address,
            person.accuracy,
            person.datetime,
            person.latitude,
            person.longitude,
        )

    @classmethod
    def from_attributes(cls, attrs: Mapping[str, Any]) -> Self:
        """Initialize location data from state attributes."""
        return cls(
            attrs[ATTR_ADDRESS],
            attrs[ATTR_GPS_ACCURACY],
            dt_util.parse_datetime(attrs[ATTR_LAST_SEEN], raise_on_error=True),
            attrs[ATTR_LATITUDE],
            attrs[ATTR_LONGITUDE],
        )


@dataclass(frozen=True)
class MiscData:
    """Miscellaneous data."""

    battery_charging: bool
    battery_level: int | None
    entity_picture: str
    full_name: str
    nickname: str

    @classmethod
    def from_person(cls, person: Person) -> Self:
        """Initialize miscellaneous data from Person object."""
        return cls(
            person.charging,
            person.battery_level,
            person.picture_url,
            person.full_name,
            person.nickname,
        )

    @classmethod
    def from_attributes(cls, attrs: Mapping[str, Any], full_name: str) -> Self:
        """Initialize miscellaneous data from state attributes."""
        return cls(
            attrs[ATTR_BATTERY_CHARGING],
            attrs.get(ATTR_BATTERY_LEVEL),
            attrs[ATTR_ENTITY_PICTURE],
            full_name,
            attrs[ATTR_NICKNAME],
        )


@dataclass(frozen=True)
class PersonData:
    """Shared person data."""

    loc: LocationData
    misc: MiscData

    @classmethod
    def from_person(cls, person: Person) -> Self:
        """Initialize shared person data from Person object."""
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
    coordinators: dict[ConfigID, GMDataUpdateCoordinator] = field(default_factory=dict)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up config entry."""
    if not (gmi_data := cast(GMIntegData | None, hass.data.get(DOMAIN))):
        hass.data[DOMAIN] = gmi_data = GMIntegData(ConfigUniqueIDs(hass))
    unique_ids = gmi_data.unique_ids

    cid = ConfigID(entry.entry_id)
    cookies_file = str(cookies_file_path(hass, entry.data[CONF_COOKIES_FILE]))
    username = entry.data[CONF_USERNAME]
    create_acct_entity = entry.options[CONF_CREATE_ACCT_ENTITY]
    scan_interval = entry.options[CONF_SCAN_INTERVAL]

    # For "account person", unique ID is username (which is also returned in person.id.)
    ent_reg = er.async_get(hass)
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

    service: Service | None = None
    get_people_func: Callable[[], Iterable[Person]]

    async def update_method() -> GMData:
        """Get shared location data."""
        nonlocal service, get_people_func

        try:
            if not service:
                service = cast(
                    Service,
                    await hass.async_add_executor_job(Service, cookies_file, username),
                )
                if create_acct_entity:
                    get_people_func = service.get_all_people
                else:
                    get_people_func = service.get_shared_people
            people = await hass.async_add_executor_job(get_people_func)
            return {person.id: PersonData.from_person(person) for person in people}
        except (InvalidCookieFile, InvalidCookies) as err:
            raise ConfigEntryAuthFailed(err) from err
        except InvalidData as err:
            raise UpdateFailed(err) from err

    coordinator = GMDataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"Google Maps ({entry.title})",
        update_interval=timedelta(seconds=scan_interval),
        update_method=update_method,
    )
    await coordinator.async_config_entry_first_refresh()

    gmi_data.coordinators[cid] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
    if unload_ok:
        gmi_data = cast(GMIntegData, hass.data[DOMAIN])
        del gmi_data.coordinators[cast(ConfigID, entry.entry_id)]
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a config entry."""
    gmi_data = cast(GMIntegData, hass.data[DOMAIN])
    gmi_data.unique_ids.remove(cast(ConfigID, entry.entry_id))
    if not gmi_data.coordinators and gmi_data.unique_ids.empty:
        del hass.data[DOMAIN]
    hass.async_add_executor_job(
        partial(
            cookies_file_path(hass, entry.data[CONF_COOKIES_FILE]).unlink,
            missing_ok=True,
        )
    )
