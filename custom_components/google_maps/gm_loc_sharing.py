"""Google Maps Location Sharing."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from http.cookiejar import LoadError, MozillaCookieJar
import json
import logging
from typing import Any, Self, cast

from requests import Session
from requests.adapters import HTTPAdapter
from urllib3 import Retry

_LOGGER = logging.getLogger(__name__)
_PROTOCOL = "https://"
_URL = f"{_PROTOCOL}www.google.com/maps/rpc/locationsharing/read"
_PARAMS: dict[str, Any] = {
    "authuser": 2,
    "hl": "en",
    "gl": "us",
    # pd holds the information about the rendering of the map and
    # it is irrelevant with the location sharing capabilities.
    # the below info points to google's headquarters.
    "pb": (
        "!1m7!8m6!1m3!1i14!2i8413!3i5385!2i6!3x4095"
        "!2m3!1e0!2sm!3i407105169!3m7!2sen!5e1105!12m4"
        "!1e68!2m2!1sset!2sRoadmap!4e1!5m4!1e4!8m2!1e0!"
        "1e1!6m9!1e12!2i2!26m1!4b1!30m1!"
        "1f1.3953487873077393!39b1!44e1!50e0!23i4111425"
    ),
}

_HTTP_PAYLOAD_TOO_LARGE = 413
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_INTERNAL_SERVER_ERROR = 500
_HTTP_BAD_GATEWAY = 502
_HTTP_SERVER_UNAVAILABLE = 503

_RETRIES_TOTAL = 5
_RETRIES_STATUSES = frozenset(
    {
        _HTTP_BAD_GATEWAY,
        _HTTP_INTERNAL_SERVER_ERROR,
        _HTTP_PAYLOAD_TOO_LARGE,
        _HTTP_SERVER_UNAVAILABLE,
        _HTTP_TOO_MANY_REQUESTS,
    }
)
_RETRIES_BACKOFF = 0.25

_VALID_COOKIE_NAMES = {"__Secure-1PSID", "__Secure-3PSID"}


class GMError(Exception):
    """Google Maps location sharing base exception."""


class InvalidCookies(GMError):
    """Invalid cookies."""


class InvalidCookiesFile(GMError):
    """Invalid cookies file."""


class InvalidData(GMError):
    """Invalid data from server."""


@dataclass
class GMPerson:
    """Person's location data."""

    id: str

    # Attributes associated with last_seen
    address: str
    country_code: str
    gps_accuracy: int
    last_seen: datetime
    latitude: float
    longitude: float

    battery_charging: bool | None
    battery_level: int | None
    full_name: str
    nickname: str
    picture_url: str | None

    def __post_init__(self) -> None:
        """Post initialization."""
        self.last_seen = datetime.fromtimestamp(
            int(self.last_seen) / 1000,  # type: ignore[call-overload]
            tz=UTC,
        )

    @classmethod
    def _shared_from_data(cls, data: list[Any]) -> Self:
        """Initialize shared person from server data."""
        try:
            battery_charging = bool(data[13][0])
        except (IndexError, TypeError):
            battery_charging = None
        try:
            battery_level = data[13][1]
        except (IndexError, TypeError):
            battery_level = None
        return cls(
            data[6][0],
            data[1][4],
            data[1][6],
            data[1][3],
            data[1][2],
            data[1][1][2],
            data[1][1][1],
            battery_charging,
            battery_level,
            data[6][2],
            data[6][3],
            data[6][1],
        )

    @classmethod
    def _acct_from_data(cls, data: list[Any], account_email: str) -> Self | None:
        """Initialize shared person from server data."""
        try:
            return cls(
                account_email,
                data[9][1][4],
                data[9][1][6],
                data[9][1][3],
                data[9][1][2],
                data[9][1][1][2],
                data[9][1][1][1],
                None,
                None,
                account_email,
                account_email,
                None,
            )
        except IndexError:
            _LOGGER.debug(
                "Information not available for holder of account: %s", account_email
            )
            return None

    @classmethod
    def people_from_data(cls, data: list[Any], account_email: str | None) -> list[Self]:
        """Return list of location data for people from server data."""
        people: list[Self] = [
            cls._shared_from_data(person_data) for person_data in data[0] or []
        ]
        if account_email is not None:
            if acct_person := cls._acct_from_data(data, account_email):
                people.append(acct_person)
        return people


CookieData = dict[str, tuple[int | None, str | None]]


class GMLocSharing:
    """Google Maps location sharing."""

    _cookies_file_data: CookieData
    _data: list[Any]

    def __init__(self, account_email: str) -> None:
        """Initialize API."""
        self._account_email = account_email
        self._session = Session()
        self._session.mount(
            _PROTOCOL,
            HTTPAdapter(
                max_retries=Retry(
                    total=_RETRIES_TOTAL,
                    status_forcelist=_RETRIES_STATUSES,
                    backoff_factor=_RETRIES_BACKOFF,
                )
            ),
        )
        self._session.cookies = MozillaCookieJar()  # type: ignore[assignment]

    @cached_property
    def _cookies(self) -> MozillaCookieJar:
        """Return session's cookies."""
        return cast(MozillaCookieJar, self._session.cookies)

    @property
    def _cookie_data(self) -> CookieData:
        """Return pertient data for current cookies."""
        return {cookie.name: (cookie.expires, cookie.value) for cookie in self._cookies}

    @property
    def cookies_changed(self) -> bool:
        """Return if cookies have changed since they were loaded or last saved."""
        return self._cookie_data != self._cookies_file_data

    @property
    def cookies_expiration(self) -> datetime | None:
        """Return expiration of 'important' cookies."""
        cookie_data = self._cookie_data
        expirations: list[int] = []
        for name in _VALID_COOKIE_NAMES:
            if (data := cookie_data.get(name)) and (expiration := data[0]):
                expirations.append(expiration)
        if not expirations:
            return None
        return datetime.fromtimestamp(min(expirations), tz=UTC)

    def close(self) -> None:
        """Close API."""
        self._session.close()

    def load_cookies(self, cookies_file: str) -> None:
        """Load cookies from file."""
        self._cookies.clear()
        try:
            self._cookies.load(cookies_file)
        except (FileNotFoundError, LoadError) as err:
            raise InvalidCookiesFile(str(err)) from None
        if not {cookie.name for cookie in self._cookies} & _VALID_COOKIE_NAMES:
            raise InvalidCookies(f"Missing either of {_VALID_COOKIE_NAMES} cookies")
        self._cookies_file_data = self._cookie_data

    def save_cookies(self, cookies_file: str) -> None:
        """Save cookies to file."""
        self._cookies.save(cookies_file)
        self._cookies_file_data = self._cookie_data

    def dump_cookies(self) -> None:
        """Dump cookies & expiration dates to log."""
        data: list[tuple[str, datetime | None]] = []
        for cookie in self._cookies:
            if cookie.expires:
                expiration = datetime.fromtimestamp(cookie.expires)
            else:
                expiration = None
            data.append((cookie.name, expiration))
        data.sort(key=lambda d: d[0])
        data.sort(key=lambda d: datetime.min if d[1] is None else d[1])
        _LOGGER.debug(
            "%s: Cookies: %s",
            self._account_email,
            ", ".join([f"{name}: {exp}" for name, exp in data]),
        )

    def get_new_data(self) -> None:
        """Get new data from Google server."""
        resp = self._session.get(_URL, params=_PARAMS, verify=True)
        resp.raise_for_status()
        raw_data = resp.text
        try:
            self._data = json.loads(raw_data[5:])
        except (IndexError, json.JSONDecodeError) as err:
            raise InvalidData(f"Could not parse: {raw_data}") from err
        try:
            if self._data[6] == "GgA=":
                raise InvalidCookies("Invalid session indicated")
        except IndexError:
            raise InvalidData(f"Unexpected parsed data: {self._data}") from None

    def get_people(self, include_acct_person: bool) -> list[GMPerson]:
        """Get people from data."""
        return GMPerson.people_from_data(
            self._data, self._account_email if include_acct_person else None
        )
