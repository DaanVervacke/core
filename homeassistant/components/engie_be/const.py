"""Constants for the ENGIE Belgium integration."""

from datetime import timedelta
from enum import StrEnum
import logging
from zoneinfo import ZoneInfo

DOMAIN = "engie_be"
ATTRIBUTION = "Data provided by ENGIE Belgium"

LOGGER = logging.getLogger(__package__)

CONF_MFA_METHOD = "mfa_method"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_HISTORY = "history"

USER_MANAGEMENT_URL = (
    "https://www.engie.be/nl/energiedesk/usermanagement/manage-access/"
)

CONTRACTS_RETRY_INTERVAL = timedelta(minutes=5)

PRICES_SCAN_INTERVAL = timedelta(hours=1)
EPEX_SCAN_INTERVAL = timedelta(hours=1)
STATISTICS_SCAN_INTERVAL = timedelta(hours=12)

BRUSSELS_TIME_ZONE = ZoneInfo("Europe/Brussels")

ATTR_PRICE = "price"


class HistoryDepth(StrEnum):
    """How far back usage history is imported into long-term statistics."""

    NONE = "none"
    MONTH = "month"
    YEAR = "year"
    ALL = "all"
