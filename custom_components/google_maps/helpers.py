"""Google Maps helper functions, etc."""
from __future__ import annotations

from dataclasses import asdict as dc_asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NewType, Self, cast

from homeassistant.components.device_tracker import DOMAIN as DT_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.util import dt as dt_util, slugify
from homeassistant.util.hass_dict import HassKey

from .const import COOKIE_WARNING_PERIOD, CREDENTIALS_FILE, DOMAIN
from .gm_loc_sharing import GMPerson

CFG_UNIQUE_IDS: HassKey[ConfigUniqueIDs] = HassKey(DOMAIN)


def old_cookies_file_path(hass: HomeAssistant, username: str) -> Path:
    """Return path to cookies file from legacy implementation."""
    return Path(hass.config.path()) / f"{CREDENTIALS_FILE}.{slugify(username)}"


def cookies_file_path(hass: HomeAssistant, cookies_file: str) -> Path:
    """Return path to cookies file."""
    return Path(hass.config.path()) / STORAGE_DIR / DOMAIN / cookies_file


def exp_2_str(expiration: datetime | None) -> str:
    """Convert expiration to a string in local time zone."""
    return str(dt_util.as_local(expiration)) if expiration is not None else "unknown"


def expiring_soon(expiration: datetime | None) -> bool:
    """Return if cookies are expiring soon."""
    return (
        expiration is not None and expiration - dt_util.utcnow() < COOKIE_WARNING_PERIOD
    )


ConfigID = NewType("ConfigID", str)
UniqueID = NewType("UniqueID", str)


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

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        """Initialize location data from a dict."""
        last_seen: datetime | str | None
        try:
            last_seen = cast(datetime | str, restored["last_seen"])
            if not isinstance(last_seen, datetime):
                last_seen = dt_util.parse_datetime(last_seen)
            if isinstance(last_seen, datetime):
                last_seen = dt_util.as_utc(last_seen)
        except (KeyError, TypeError):
            return None
        if last_seen is None:
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
            dt_util.utc_from_timestamp(person.last_seen),
            person.latitude,
            person.longitude,
        )


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
                if ent.domain == DT_DOMAIN
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
