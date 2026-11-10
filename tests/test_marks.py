"""Table-driven tests for mark validation. All values are invented."""

import pytest

from bestyet import marks
from bestyet.marks import parse_mark


def grist_value(mark: str) -> float | None:
    """The Results.Value reference formula from SPEC.md, applied to a stored Mark."""
    s = str(mark).strip().lower().rstrip("ms").replace(",", ".")
    if not s:
        return None
    if ":" in s:
        m, sec = s.split(":")
        return int(m) * 60 + float(sec)
    return float(s)


VALID = [
    # input, normalised, value
    ("2.98", "2.98", 2.98),
    ("13.45", "13.45", 13.45),
    ("7", "7", 7.0),
    ("2:15.30", "2:15.30", 135.3),
    ("2:15", "2:15", 135.0),
    ("10:05.5", "10:05.5", 605.5),
    ("007", "7", 7.0),
    ("007.50", "7.50", 7.5),
    ("0.5", "0.5", 0.5),
    ("00:30", "0:30", 30.0),
    ("0:59.99", "0:59.99", 59.99),
    ("  13.45  ", "13.45", 13.45),
    ("2,98", "2.98", 2.98),
    ("2:15,30", "2:15.30", 135.3),
    ("13.45s", "13.45", 13.45),
    ("13.45S", "13.45", 13.45),
    ("5.20m", "5.20", 5.2),
    ("5.20M", "5.20", 5.2),
    ("5.20 m", "5.20", 5.2),
    (" 2:15.30 ", "2:15.30", 135.3),
    ("02:15.30", "2:15.30", 135.3),
    ("0.01", "0.01", 0.01),
]


@pytest.mark.parametrize(("text", "normalised", "value"), VALID)
def test_valid(text, normalised, value):
    r = parse_mark(text)
    assert r.ok
    assert r.normalised == normalised
    assert r.value == pytest.approx(value)
    assert r.warning is None
    assert r.error is None


@pytest.mark.parametrize(("text", "normalised", "value"), VALID)
def test_value_matches_grist_formula(text, normalised, value):
    r = parse_mark(text)
    assert grist_value(r.normalised) == r.value


INVALID = [
    ("", marks.MSG_EMPTY),
    ("   ", marks.MSG_EMPTY),
    ("abc", marks.MSG_INVALID),
    ("m", marks.MSG_INVALID),
    ("-2.98", marks.MSG_INVALID),
    ("+2.98", marks.MSG_INVALID),
    ("1.2.3", marks.MSG_INVALID),
    ("1:2:3", marks.MSG_INVALID),
    ("2:", marks.MSG_INVALID),
    (":30", marks.MSG_INVALID),
    ("2.", marks.MSG_INVALID),
    (".5", marks.MSG_INVALID),
    ("2.98ms", marks.MSG_INVALID),
    ("2.98ss", marks.MSG_INVALID),
    ("2.98 sec", marks.MSG_INVALID),
    ("2:15.30m", marks.MSG_UNIT),
    ("2:15.30 s", marks.MSG_UNIT),
    ("2:5", marks.MSG_SEC_DIGITS),
    ("2:5.5", marks.MSG_SEC_DIGITS),
    ("2:150", marks.MSG_SEC_DIGITS),
    ("2.1234567", marks.MSG_PRECISION),
    ("2:15.1234567", marks.MSG_PRECISION),
    ("00", marks.MSG_ZERO),
    ("nan", marks.MSG_INVALID),
    ("inf", marks.MSG_INVALID),
    ("-inf", marks.MSG_INVALID),
    ("1e3", marks.MSG_INVALID),
    ("1_000", marks.MSG_INVALID),
    ("1,000.5", marks.MSG_INVALID),
    ("2:15:30", marks.MSG_INVALID),
    ("2:-5", marks.MSG_INVALID),
    ("\u0662.\u0669", marks.MSG_INVALID),  # non-ASCII digits
    ("9" * 400, marks.MSG_INVALID),
    ("2.98\n13", marks.MSG_INVALID),
    ("2:60", marks.MSG_SECONDS),
    ("1:75.0", marks.MSG_SECONDS),
    ("0", marks.MSG_ZERO),
    ("0.00s", marks.MSG_ZERO),
    ("0:00", marks.MSG_ZERO),
]


@pytest.mark.parametrize(("text", "message"), INVALID)
def test_invalid(text, message):
    r = parse_mark(text)
    assert not r.ok
    assert r.error == message
    assert r.value is None
    assert r.normalised is None
    assert r.warning is None


@pytest.mark.parametrize(
    ("text", "normalised"),
    [("2.981", "2.981"), ("2.980", "2.980"), ("2:15.305", "2:15.305"), ("2.123456", "2.123456")],
)
def test_more_than_two_decimals_warns_but_is_allowed(text, normalised):
    r = parse_mark(text)
    assert r.ok
    assert r.normalised == normalised
    assert r.warning == marks.MSG_DECIMALS
    assert grist_value(r.normalised) == r.value


def test_two_decimals_do_not_warn():
    assert parse_mark("2.98").warning is None


def test_messages_are_short_single_line_sentences():
    msgs = [
        marks.MSG_EMPTY,
        marks.MSG_INVALID,
        marks.MSG_SECONDS,
        marks.MSG_ZERO,
        marks.MSG_DECIMALS,
        marks.MSG_SEC_DIGITS,
        marks.MSG_UNIT,
        marks.MSG_PRECISION,
    ]
    for msg in msgs:
        assert msg.endswith(".")
        assert "\n" not in msg
        assert len(msg) <= 80


def test_result_is_frozen():
    with pytest.raises(AttributeError):
        parse_mark("2.98").ok = False
