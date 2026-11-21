"""Constants for Realtime Trains."""

from datetime import timedelta
from zoneinfo import ZoneInfo

DOMAIN = "realtime_trains"
NAME = "Realtime Trains"
BASE_URL = "https://data.rtt.io"
API_VERSION = "2026-04-09"
TOKEN_REFRESH_LEAD_TIME = 60
UK_TIME_ZONE = ZoneInfo("Europe/London")

CONF_TOKEN = "token"
CONF_IS_REFRESH_TOKEN = "is_refresh_token"
CONF_STATION = "station"
CONF_STATION_NAME = "station_name"
CONF_STATIONS = "stations"
CONF_TRACKS = "tracks"
CONF_DIRECTION = "direction"
CONF_WINDOW_START = "window_start"
CONF_TRACK = "track"

BOARD_CACHE_TTL = timedelta(seconds=60)
STATIONS_CACHE_TTL = timedelta(days=1)
ERROR_RETRY_INTERVAL = timedelta(minutes=5)
STORE_VERSION = 1
