"""Constants for Google Maps Integration."""
from datetime import timedelta

DOMAIN = "google_maps"
ATTRIBUTION = "Data from Google Maps"

CREDENTIALS_FILE = ".google_maps_location_sharing.cookies"

DEF_SCAN_INTERVAL_SEC = 60
DEF_SCAN_INTERVAL = timedelta(seconds=DEF_SCAN_INTERVAL_SEC)
MISSING_DATA_GRACE_PERIOD = 3 * 60
COOKIE_WARNING_PERIOD = timedelta(weeks=4)

ATTR_ADDRESS = "address"
ATTR_LAST_SEEN = "last_seen"
ATTR_NICKNAME = "nickname"

CONF_COOKIES_FILE = "cookies_file"
CONF_CREATE_ACCT_ENTITY = "create_acct_entity"
CONF_MAX_GPS_ACCURACY = "max_gps_accuracy"
