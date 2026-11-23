"""Google Maps Location Sharing."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.cookiejar import MozillaCookieJar
import json
import logging
import re
from typing import Any, Self, cast
from urllib.parse import parse_qs, urlparse

from propcache.api import cached_property
from requests import RequestException, Session
from requests.adapters import HTTPAdapter
from urllib3 import Retry
from urllib3.exceptions import MaxRetryError

_LOGGER = logging.getLogger(__name__)
_PROTOCOL = "https://"
_URL_GOOGLE = f"{_PROTOCOL}www.google.com"
_URL_GOOGLE_MAPS = f"{_URL_GOOGLE}/maps"
_URL_ACCOUNTS = f"{_PROTOCOL}accounts.google.com"
_URL_ROTATE_COOKIES_PAGE = f"{_URL_ACCOUNTS}/RotateCookiesPage"
_URL_ROTATE_COOKIES = f"{_URL_ACCOUNTS}/RotateCookies"
_URL = f"{_URL_GOOGLE_MAPS}/rpc/locationsharing/read"
_ROTATE_INTERVAL = timedelta(minutes=10)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/91.0.4472.114 Safari/537.36"
)
_OG_PID_REGEX = r'\\"(https://accounts\.google\.com/ListAccounts\?[^"]+)\\"'
_EXP_ID_REGEX = (
    r'\\"https://accounts\.google\.com/RotateCookiesPage\\"],'
    r"([0-9]+),[^,]+,[^,]+,[^,]+,([0-9]+),"
)
_INIT_VALUE_REGEX = r'init\(\'(-?\d+)\','
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
_TIMEOUT = 60

_VALID_COOKIE_NAMES = {"__Secure-1PSID", "__Secure-3PSID"}


class GMError(Exception):
    """Google Maps location sharing base exception."""


class InvalidCookies(GMError):
    """Invalid cookies."""


class InvalidCookiesFile(GMError):
    """Invalid cookies file."""


class InvalidData(GMError):
    """Invalid data from server."""


class RequestFailed(GMError):
    """Server request failed."""


@dataclass
class GMPerson:
    """Person's location data."""

    id: str

    # Attributes associated with last_seen
    address: str
    country_code: str
    gps_accuracy: int
    last_seen: float
    latitude: float
    longitude: float

    battery_charging: bool | None
    battery_level: int | None
    full_name: str
    nickname: str
    picture_url: str | None

    def __post_init__(self) -> None:
        """Post initialization."""
        self.last_seen = int(self.last_seen) / 1000

    @classmethod
    def shared_from_data(cls, data: Sequence[Any]) -> Self | None:
        """Initialize shared person from server data."""
        try:
            battery_charging = bool(data[13][0])
        except (IndexError, TypeError):
            battery_charging = None
        try:
            battery_level = data[13][1]
        except (IndexError, TypeError):
            battery_level = None
        try:
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
        except (IndexError, TypeError):
            return None

    @classmethod
    def acct_from_data(cls, data: Sequence[Any], account_email: str) -> Self | None:
        """Initialize account holder from server data.

        id wll be account's email address.
        """
        try:
            return cls(
                account_email,
                data[1][4],
                data[1][6],
                data[1][3],
                data[1][2],
                data[1][1][2],
                data[1][1][1],
                None,
                None,
                account_email,
                account_email,
                None,
            )
        except (IndexError, TypeError):
            return None


CookieData = dict[str, tuple[int | None, str | None]]


class GMLocSharing:
    """Google Maps location sharing."""

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
        self._cookies_file_data: CookieData = {}
        self._cookies_file: str | None = None
        self._data: Sequence[Any] = []
        self._last_cookie_rotation: datetime | None = None

    @cached_property
    def _cookies(self) -> MozillaCookieJar:
        """Return session's cookies."""
        return cast(MozillaCookieJar, self._session.cookies)

    @property
    def _cookie_data(self) -> CookieData:
        """Return pertinent data for current cookies."""
        return {cookie.name: (cookie.expires, cookie.value) for cookie in self._cookies}

    @property
    def cookies_changed(self) -> bool:
        """Return if cookies have changed since they were loaded or last saved."""
        return self._cookie_data != self._cookies_file_data

    @property
    def cookies_expiration(self) -> datetime | None:
        """Return expiration of 'important' cookies in UTC."""
        cookie_data = self._cookie_data
        expirations: list[int] = []
        for name in _VALID_COOKIE_NAMES:
            if (data := cookie_data.get(name)) and (expiration := data[0]):
                expirations.append(expiration)  # noqa: PERF401
        if not expirations:
            return None
        return datetime.fromtimestamp(min(expirations), UTC)

    def close(self) -> None:
        """Close API."""
        self._session.close()

    def load_cookies(self, cookies_file: str) -> None:
        """Load cookies from file."""
        self._cookies.clear()
        try:
            self._cookies.load(cookies_file)
        except OSError as err:
            raise InvalidCookiesFile(f"{err.__class__.__name__}: {err}") from None
        self._dump_cookies()
        if not {cookie.name for cookie in self._cookies} & _VALID_COOKIE_NAMES:
            raise InvalidCookies(f"Missing either of {_VALID_COOKIE_NAMES} cookies")
        self._cookies_file = str(cookies_file)
        self._cookies_file_data = self._cookie_data

    def save_cookies(self, cookies_file: str) -> None:
        """Save cookies to file."""
        self._dump_changed_cookies()
        if not {cookie.name for cookie in self._cookies} & _VALID_COOKIE_NAMES:
            # Google deletes the session cookies when it rejects a session. Do not
            # overwrite a usable cookies file with the degraded set.
            _LOGGER.warning(
                "%s: Not saving cookies because the session cookies are missing",
                self._account_email,
            )
            self._cookies_file_data = self._cookie_data
            return
        self._cookies.save(cookies_file, ignore_discard=True)
        self._cookies_file_data = self._cookie_data

    def _rotate_cookies(self) -> None:
        """Refresh the Google session using Google's RotateCookies flow.

        Best effort: if Google changes the page format or rate limits the request,
        keep using the existing cookies instead of failing the whole update.
        """
        now = datetime.now(UTC)
        if (
            self._last_cookie_rotation is not None
            and now - self._last_cookie_rotation < _ROTATE_INTERVAL
        ):
            return
        try:
            resp = self._session.get(
                _URL_GOOGLE_MAPS,
                params={"authuser": _PARAMS["authuser"]},
                timeout=_TIMEOUT,
                verify=True,
            )
            resp.raise_for_status()
            raw_data = resp.text
            if not (match := re.search(_OG_PID_REGEX, raw_data)):
                raise InvalidData("Could not find ListAccounts URL")
            url = match.group(1).encode("utf-8").decode("unicode-escape")
            url = url.encode("utf-8").decode("unicode-escape")
            og_pid = parse_qs(urlparse(url).query)["pid"][0]
            if not (match := re.search(_EXP_ID_REGEX, raw_data)):
                raise InvalidData("Could not find cookie rotation parameters")
            rot, exp_id = match.groups()
            resp = self._session.get(
                _URL_ROTATE_COOKIES_PAGE,
                params={
                    "og_pid": og_pid,
                    "rot": rot,
                    "origin": _URL_GOOGLE,
                    "exp_id": exp_id,
                },
                headers={"Referer": _URL_GOOGLE, "User-Agent": _USER_AGENT},
                timeout=_TIMEOUT,
                verify=True,
            )
            resp.raise_for_status()
            update_url = resp.url
            if not (match := re.search(_INIT_VALUE_REGEX, resp.text)):
                raise InvalidData("Could not find cookie rotation init value")
            resp = self._session.post(
                _URL_ROTATE_COOKIES,
                json=[og_pid, match.group(1)],
                headers={"Referer": update_url, "User-Agent": _USER_AGENT},
                timeout=_TIMEOUT,
                verify=True,
            )
            resp.raise_for_status()
        except (
            AttributeError,
            IndexError,
            KeyError,
            RequestException,
            MaxRetryError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            InvalidData,
        ) as err:
            # Do not retry on every poll (e.g., after a 429 rate limit).
            self._last_cookie_rotation = now
            _LOGGER.debug(
                "%s: Cookie rotation failed: %s: %s",
                self._account_email,
                err.__class__.__name__,
                err,
            )
            return

        self._last_cookie_rotation = now
        _LOGGER.debug("%s: Rotated Google session cookies", self._account_email)
        if self._cookies_file:
            try:
                self.save_cookies(self._cookies_file)
            except OSError as err:
                _LOGGER.warning(
                    "%s: Could not save rotated cookies: %s: %s",
                    self._account_email,
                    err.__class__.__name__,
                    err,
                )

    def get_new_data(self) -> None:
        """Get new data from Google server."""
        self._rotate_cookies()
        try:
            resp = self._session.get(
                _URL, params=_PARAMS, timeout=_TIMEOUT, verify=True
            )
            resp.raise_for_status()
        except (RequestException, MaxRetryError) as err:
            raise RequestFailed(f"{err.__class__.__name__}: {err}") from err
        raw_data = resp.text
        try:
            self._data = json.loads(raw_data[5:])
        except (IndexError, json.JSONDecodeError) as err:
            raise InvalidData(f"Could not parse: {raw_data}") from err
        if not isinstance(self._data, Sequence):
            raise InvalidData(f"Expected a Sequence, got: {self._data}")
        try:
            if self._data[6] == "GgA=":
                self._dump_cookies()
                _LOGGER.debug("%s: Parsed data: %s", self._account_email, self._data)
                raise InvalidCookies("Invalid session indicated")
        except IndexError:
            raise InvalidData(f"Unexpected parsed data: {self._data}") from None

    def get_people(self, include_acct_person: bool) -> list[GMPerson]:
        """Get people from data.

        If including "account person", its id will be the account's email address.
        """
        people: list[GMPerson] = []
        bad_data: list[list[Any]] = []
        if len(self._data) < 1:
            raise InvalidData("No shared location data")
        for person_data in self._data[0] or []:
            if person := GMPerson.shared_from_data(person_data):
                people.append(person)
            else:
                bad_data.append(person_data)
        if include_acct_person and len(self._data) >= 10:
            if person := GMPerson.acct_from_data(self._data[9], self._account_email):
                people.append(person)
            else:
                bad_data.append(self._data[9])
        for bad_person_data in bad_data:
            _LOGGER.debug(
                "%s: Missing location or other data for person: %s",
                self._account_email,
                bad_person_data,
            )
        return people

    def _dump_cookies(self) -> None:
        """Dump cookies & expiration dates to log."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        data: list[tuple[str, datetime | None]] = []
        for cookie in self._cookies:
            if cookie.expires:
                expiration = datetime.fromtimestamp(cookie.expires)
            else:
                expiration = None
            data.append((cookie.name, expiration))
        data.sort(key=lambda d: (datetime.max if d[1] is None else d[1], d[0]))
        _LOGGER.debug(
            "%s: Cookies: %s",
            self._account_email,
            ", ".join([f"{name}: {exp}" for name, exp in data]),
        )

    def _dump_changed_cookies(self) -> None:
        """Dump cookie changes since last saved to log."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        msg: list[str] = []
        cookie_data = self._cookie_data
        cur_names = set(cookie_data)
        saved_names = set(self._cookies_file_data)
        if dropped := saved_names - cur_names:
            msg.append(f"dropped: {', '.join(dropped)}")
        diff = {
            name
            for name in cur_names & saved_names
            if cookie_data[name] != self._cookies_file_data[name]
        }
        if diff:
            msg.append(f"updated: {', '.join(diff)}")
        if new := cur_names - saved_names:
            msg.append(f"new: {', '.join(new)}")
        _LOGGER.debug(
            "%s: Changed cookies since last saved: %s",
            self._account_email,
            ", ".join(msg),
        )
