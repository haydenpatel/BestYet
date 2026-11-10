"""Validate and parse a typed mark before it is sent to Grist.

The Grist ``Results.Value`` formula throws on junk, so every mark is checked
here first. Accepted formats (surrounding whitespace is ignored):

* a plain number: ``2.98``, ``13.45``, ``7`` (seconds, or metres for a throw or jump)
* ``m:ss.ss``: ``2:15.30`` (minutes, then exactly two digits of seconds below 60,
  decimals optional)
* a comma may be used as the decimal separator: ``2,98``
* on a plain number only, one trailing unit, ``m`` or ``s`` in either case,
  optionally after a space: ``2.98s``, ``5.20 m`` (not allowed on ``m:ss``)

Marks must be greater than zero, with at most six decimals. More than two
decimals is allowed but warns.

On success ``normalised`` is the text to store: ``.`` as the separator, no unit
and no leading zeros (``007`` becomes ``7``, ``02:15.30`` becomes ``2:15.30``).
The Grist formula turns that text into exactly ``value``.
"""

import math
import re
from dataclasses import dataclass

_PLAIN = re.compile(r"(?P<whole>[0-9]{1,6})(?:[.,](?P<frac>[0-9]+))?")
_MINUTES = re.compile(r"(?P<min>[0-9]{1,4}):(?P<sec>[0-9]+)(?:[.,](?P<frac>[0-9]+))?")
_MAX_DECIMALS = 6

MSG_EMPTY = "Enter a mark, for example 2.98 or 2:15.30."
MSG_INVALID = "Not a valid mark. Use a number like 2.98, or minutes like 2:15.30."
MSG_SECONDS = "Seconds must be below 60 in m:ss format, for example 2:15.30."
MSG_SEC_DIGITS = "Use two digits for seconds in m:ss format, for example 2:05.30."
MSG_UNIT = "Leave the unit off an m:ss mark, for example 2:15.30."
MSG_PRECISION = "Too many decimals; use at most six."
MSG_ZERO = "A mark must be greater than zero."
MSG_DECIMALS = "More than two decimals; check the mark is correct."


@dataclass(frozen=True)
class MarkResult:
    ok: bool
    value: float | None = None  # seconds or metres, as the Grist formula computes it
    normalised: str | None = None  # text to store in Mark
    warning: str | None = None
    error: str | None = None


def _fail(message: str) -> MarkResult:
    return MarkResult(ok=False, error=message)


def parse_mark(text: str) -> MarkResult:
    """Check ``text`` and return its value and normalised form, or an error."""
    s = text.strip()
    if not s:
        return _fail(MSG_EMPTY)
    unit = s[-1] in "msMS"
    if unit:
        s = s[:-1].rstrip()

    if m := _MINUTES.fullmatch(s):
        if unit:
            return _fail(MSG_UNIT)
        if len(m["sec"]) != 2:
            return _fail(MSG_SEC_DIGITS)
        if int(m["sec"]) >= 60:
            return _fail(MSG_SECONDS)
        frac = m["frac"] or ""
        number = f"{m['min'].lstrip('0') or '0'}:{m['sec']}"
        value = int(m["min"]) * 60 + float(f"{m['sec']}.{frac or '0'}")
    elif m := _PLAIN.fullmatch(s):
        frac = m["frac"] or ""
        number = m["whole"].lstrip("0") or "0"
        value = float(f"{m['whole']}.{frac or '0'}")
    else:
        return _fail(MSG_INVALID)

    if len(frac) > _MAX_DECIMALS:
        return _fail(MSG_PRECISION)
    if frac:
        number += f".{frac}"

    if not math.isfinite(value):  # defensive; the patterns already bound the size
        return _fail(MSG_INVALID)
    if value <= 0:
        return _fail(MSG_ZERO)
    warning = MSG_DECIMALS if len(frac) > 2 else None
    return MarkResult(ok=True, value=value, normalised=number, warning=warning)
