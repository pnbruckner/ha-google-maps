"""Google Maps helper functions, etc."""

from collections import defaultdict
from dataclasses import asdict as dc_asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NewType, Self, cast, override

from homeassistant.components.device_tracker import DOMAIN as DT_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import ExtraStoredData
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.util import dt as dt_util, slugify
from homeassistant.util.hass_dict import HassKey

from .const import COOKIE_WARNING_PERIOD, CREDENTIALS_FILE, DOMAIN
from .gm_loc_sharing import GMPerson

CFG_PERSON_IDS: HassKey[ConfigPersonIDs] = HassKey(DOMAIN)
ConfigID = NewType("ConfigID", str)
PersonID = NewType("PersonID", str)


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


def dev_ids(uid: ConfigID | PersonID) -> set[tuple[str, str]]:
    """Return device identifiers.

    For the service associated with the account's binary sensor, use the ConfigID.
    For a device associated with a person, use the PersonID.
    """
    return {(DOMAIN, uid)}


class FromAttributesError(Exception):
    """Cannot create object from state attributes."""


@dataclass(frozen=True)
class LocationData(ExtraStoredData):
    """Location data."""

    address: str
    gps_accuracy: int
    last_seen: datetime
    latitude: float
    longitude: float

    @override
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
class PersonData:
    """Shared person data."""

    loc: LocationData
    misc: MiscData

    @classmethod
    def from_person(cls, person: GMPerson) -> Self:
        """Initialize shared person data from GMPerson object."""
        return cls(
            LocationData.from_person(person),
            MiscData.from_person(person),
        )


class ConfigPersonIDs:
    """Person ID config assignments.

    Since multiple Google accounts might be added, and it's possible for people to have
    shared their location with more than one of those accounts, to avoid having the same
    Entity being created by more than one account (i.e., ConfigEntry), keep a record of
    which config each entity is associated with. This will not only avoid having to keep
    querying the Entity Registry, it will also avoid race conditions where multiple
    configs might try to create an Entity for the same shared person at the same time.

    Do not consider disabled config entries. Any entity registered with a disabled
    config entry will be "up for grabs" and can be "taken" by another, enabled config
    entry. Also, if/when a config entry gets disabled, any entities it did own must be
    released as well. If/when it gets re-enabled, it can attempt to take any entities it
    used to own if another config entry hasn't taken them in the meantime.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize assignments from Entity Registry."""
        self._all_pids: set[PersonID] = set()
        self._cfg_pids: defaultdict[ConfigID, set[PersonID]] = defaultdict(set)

        ent_reg = er.async_get(hass)
        for cfg in hass.config_entries.async_entries(DOMAIN, include_disabled=False):
            cid = ConfigID(cfg.entry_id)
            cfg_pids = {
                # Tracker entity uses person ID as its unique ID.
                PersonID(ent.unique_id)
                for ent in er.async_entries_for_config_entry(ent_reg, cid)
                if ent.domain == DT_DOMAIN
            }
            if not cfg_pids:
                continue
            self._all_pids.update(cfg_pids)
            self._cfg_pids[cid] = cfg_pids

    def owned(self, cid: ConfigID) -> frozenset[PersonID]:
        """Return person IDs owned by config."""
        return frozenset(self._cfg_pids.get(cid, set()))

    def take(self, cid: ConfigID, pids: frozenset[PersonID]) -> frozenset[PersonID]:
        """Take ownership of a set of person IDs.

        Returns set of person IDs actually taken;
        i.e., that did not already belong to other configs.
        """
        pids -= self._all_pids - self.owned(cid)
        self._all_pids.update(pids)
        self._cfg_pids[cid].update(pids)
        return pids

    def release(self, cid: ConfigID, pid: PersonID) -> None:
        """Release ownership of a single person ID if owned by config."""
        if pid not in self.owned(cid):
            return
        self._all_pids.remove(pid)
        self._cfg_pids[cid].remove(pid)
        if not self._cfg_pids[cid]:
            del self._cfg_pids[cid]

    def release_all(self, cid: ConfigID) -> None:
        """Release all person IDs owned by config."""
        self._all_pids -= self._cfg_pids.pop(cid, set())
