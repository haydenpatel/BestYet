import time
from datetime import UTC, date, datetime, timedelta

import pytest

from bestyet import dates

ODD_ZONES = ["UTC", "Pacific/Kiritimati", "Pacific/Pago_Pago", "Asia/Kolkata", "America/St_Johns"]

SAMPLE = [
    date(1970, 1, 1),
    date(1969, 12, 31),
    date(2000, 2, 29),
    date(2024, 2, 29),
    date(2026, 11, 1),
    date(2026, 12, 31),
    date(2027, 1, 1),
    date(2100, 3, 1),
    date(9999, 12, 31),
] + [date(2020, 1, 1) + timedelta(days=n) for n in range(0, 3000, 7)]


@pytest.fixture(params=ODD_ZONES)
def process_tz(request, monkeypatch):
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


def test_known_value():
    assert dates.to_grist(date(2026, 11, 1)) == 1793491200
    assert dates.to_grist(date(1970, 1, 1)) == 0


def test_round_trip_in_any_process_zone(process_tz):
    for d in SAMPLE:
        value = dates.to_grist(d)
        assert value % 86400 == 0
        assert value == int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())
        assert dates.from_grist(value) == d


def test_from_grist_accepts_floats():
    assert dates.from_grist(1793491200.0) == date(2026, 11, 1)


def test_from_grist_floors_to_utc_date():
    assert dates.from_grist(1793491200 + 86399) == date(2026, 11, 1)
    assert dates.from_grist(1793491200 - 1) == date(2026, 10, 31)


@pytest.mark.parametrize(
    "value", [None, "", "1793491200", True, [], {}, float("nan"), float("inf"), 1e30, -1e30]
)
def test_from_grist_invalid_is_none(value):
    assert dates.from_grist(value) is None


def test_today_uses_the_named_zone():
    for tz in ODD_ZONES:
        a, b = dates.today(tz), dates.today("UTC")
        assert abs((a - b).days) <= 1
    assert dates.today("Pacific/Kiritimati") >= dates.today("Pacific/Pago_Pago")


def test_today_ignores_process_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    time.tzset()
    try:
        assert dates.today("UTC") == datetime.now(UTC).date()
    finally:
        monkeypatch.undo()
        time.tzset()


def test_parse_and_format_iso():
    assert dates.parse_iso("2026-11-01") == date(2026, 11, 1)
    assert dates.parse_iso(" 2024-02-29 ") == date(2024, 2, 29)
    assert dates.format_iso(date(2026, 1, 5)) == "2026-01-05"
    for d in SAMPLE:
        assert dates.parse_iso(dates.format_iso(d)) == d


@pytest.mark.parametrize(
    "text",
    ["", "2026-02-30", "2025-02-29", "20261101", "2026-1-1", "01/11/2026", "x", "2026-13-01"],
)
def test_parse_iso_invalid(text):
    assert dates.parse_iso(text) is None
