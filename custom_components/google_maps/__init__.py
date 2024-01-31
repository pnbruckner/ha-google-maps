"""The google_maps component."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict as dc_asdict, dataclass, field
from datetime import datetime, timedelta
from functools import partial
import logging
from os import PathLike
from pathlib import Path
from typing import Any, NewType, Self, cast

from locationsharinglib import Person, Service
from locationsharinglib.locationsharinglib import VALID_COOKIE_NAMES
from locationsharinglib.locationsharinglibexceptions import (
    InvalidCookieFile,
    InvalidCookies,
    InvalidData,
)
from requests import RequestException, Response, Session

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
    CREDENTIALS_FILE,
    DOMAIN,
    NAME_PREFIX,
)

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = [Platform.DEVICE_TRACKER]

_UNAUTHORIZED = 401
_FORBIDDEN = 403
_TOO_MANY_REQUESTS = 429
_AUTH_ERRORS = (_UNAUTHORIZED, _FORBIDDEN)


def old_cookies_file_path(hass: HomeAssistant, username: str) -> Path:
    """Return path to cookies file from legacy implementation."""
    return Path(hass.config.path()) / f"{CREDENTIALS_FILE}.{slugify(username)}"


def cookies_file_path(hass: HomeAssistant, cookies_file: str) -> Path:
    """Return path to cookies file."""
    return Path(hass.config.path()) / STORAGE_DIR / DOMAIN / cookies_file


def get_expiration(cookies: str) -> datetime | None:
    """Return expiration of cookies."""
    return min(
        [
            dt_util.as_local(dt_util.utc_from_timestamp(int(cookie_data[4])))
            for cookie_data in [
                line.strip().split()
                for line in cookies.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if cookie_data[5] in VALID_COOKIE_NAMES
        ],
        default=None,
    )


def exp_2_str(expiration: datetime | None) -> str:
    """Convert expiration to a string."""
    return str(expiration) if expiration is not None else "unknown"


def expiring_soon(expiration: datetime | None) -> bool:
    """Return if cookies are expiring soon."""
    return expiration is not None and expiration - dt_util.now() < timedelta(weeks=4)


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
        except (KeyError, TypeError):
            raise FromAttributesError
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
    def from_person(cls, person: Person) -> Self:
        """Initialize location data from Person object."""
        return cls(
            cast(str, person.address),
            cast(int, person.accuracy),
            person.datetime,
            cast(float, person.latitude),
            cast(float, person.longitude),
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
        except KeyError:
            raise FromAttributesError


@dataclass(frozen=True)
class MiscData:
    """Miscellaneous data."""

    battery_charging: bool
    battery_level: int | None
    entity_picture: str
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
    def from_person(cls, person: Person) -> Self:
        """Initialize miscellaneous data from Person object."""
        return cls(
            person.charging,
            cast(int | None, person.battery_level),
            cast(str, person.picture_url),
            cast(str, person.full_name),
            cast(str, person.nickname),
        )

    @classmethod
    def from_attributes(cls, attrs: Mapping[str, Any], full_name: str) -> Self:
        """Initialize miscellaneous data from state attributes."""
        try:
            return cls(
                attrs[ATTR_BATTERY_CHARGING],
                attrs.get(ATTR_BATTERY_LEVEL),
                attrs[ATTR_ENTITY_PICTURE],
                full_name,
                attrs[ATTR_NICKNAME],
            )
        except KeyError:
            raise FromAttributesError


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


class GMService(Service):  # type: ignore[misc]
    """Service class with better error detection, handling & reporting."""

    def __init__(
        self, cookies_file: str | PathLike, authenticating_account: str
    ) -> None:
        """Initialize service."""
        super().__init__(cookies_file, authenticating_account)

    @staticmethod
    def _get_server_response(session: Session) -> Response:
        """Get response from server using session and check for unauthorized error."""
        resp = None
        try:
            resp = cast(Response, Service._get_server_response(session))
            resp.raise_for_status()
        except RequestException as err:
            if resp and resp.status_code in _AUTH_ERRORS:
                _LOGGER.debug(
                    "Error: %s: %i %s; reauthorize",
                    err.__class__.__name__,
                    resp.status_code,
                    resp.reason,
                )
                raise InvalidCookies(f"{err.__class__.__name__}: {err}") from err
            raise
        return resp

    def _get_data(self) -> list[str]:
        """Get data, parse and check for invalid session."""
        data = cast(
            list[str],
            self._parse_location_data(self._get_server_response(self._session).text),
        )
        try:
            if data[6] == "GgA=":
                raise InvalidCookies("Invalid session indicated")
        except IndexError:
            raise InvalidData(f"Unexpected data: {data}") from None
        return data

    # def substitute_cookies(self, cookies_file_contents: str) -> None:
    #     """Create new session with different cookies that are NOT validated."""
    #     cookie_entries = [line.strip() for line in cookies_file_contents.splitlines()
    #                         if not line.strip().startswith('#') and line]
    #     cookies = []
    #     for entry in cookie_entries:
    #         domain, flag, path, secure, expiry, name, value, *rest = entry.split()
    #         cookies.append(Cookie(domain, flag, path, secure, expiry, name, value, rest)) # type: ignore
    #     session = Session()
    #     for cookie in cookies:
    #         session.cookies.set(**cookie.to_dict())
    #     self._session = session


async def entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up config entry."""
    if not (gmi_data := cast(GMIntegData | None, hass.data.get(DOMAIN))):
        hass.data[DOMAIN] = gmi_data = GMIntegData(ConfigUniqueIDs(hass))
    unique_ids = gmi_data.unique_ids

    cid = ConfigID(entry.entry_id)
    cf_path = cookies_file_path(hass, entry.data[CONF_COOKIES_FILE])
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

    service: GMService | None = None
    get_people_func: Callable[[], Iterable[Person]]

    async def update_method() -> GMData:
        """Get shared location data."""
        nonlocal service, get_people_func

        try:
            if not service:
                service = await hass.async_add_executor_job(
                    GMService, cf_path, username
                )
                if create_acct_entity:
                    get_people_func = service.get_all_people
                else:
                    get_people_func = service.get_shared_people
            people = await hass.async_add_executor_job(get_people_func)
        except (InvalidCookieFile, InvalidCookies) as err:
            raise ConfigEntryAuthFailed(f"{err.__class__.__name__}: {err}") from err
        except (RequestException, InvalidData) as err:
            raise UpdateFailed(f"{err.__class__.__name__}: {err}") from err
        return {
            UniqueID(cast(str, person.id)): PersonData.from_person(person)
            for person in people
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

    expiration = get_expiration(await hass.async_add_executor_job(cf_path.read_text))
    if expiring_soon(expiration):
        async_create_issue(
            hass,
            DOMAIN,
            entry.entry_id,
            is_fixable=False,
            is_persistent=False,
            severity=IssueSeverity.WARNING,
            translation_key="expiring_soon",
            translation_placeholders={
                "entry_id": entry.entry_id,
                "expiration": exp_2_str(expiration),
                "username": username,
            },
        )
    else:
        async_delete_issue(hass, DOMAIN, entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(entry_updated))
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
