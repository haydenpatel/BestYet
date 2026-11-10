"""Convert calendar dates to and from Grist Date values.

A Grist Date is Unix seconds at UTC midnight. No time zone is applied in either
direction, so the date entered is the date stored and shown. The configured time
zone is only used by `today`, to pick the default date for a new meet.
"""

import math
import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_EPOCH = date(1970, 1, 1)


def to_grist(d: date) -> int:
    """The Grist Date value for a calendar date: seconds at UTC midnight."""
    return (d - _EPOCH).days * 86400


def from_grist(value: object) -> date | None:
    """The calendar date for a Grist Date value, or None for empty or invalid cells."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


def today(tz_name: str) -> date:
    """Today's calendar date in the named time zone."""
    return datetime.now(ZoneInfo(tz_name)).date()


def parse_iso(text: str) -> date | None:
    """Parse the `YYYY-MM-DD` text of a native date input, or None if it is not a real date."""
    text = text.strip()
    if not _ISO.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def format_iso(d: date) -> str:
    """The `YYYY-MM-DD` text for a native date input."""
    return d.isoformat()
