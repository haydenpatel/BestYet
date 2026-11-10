"""Validate and parse a typed mark before it is sent to Grist.

The Grist ``Results.Value`` formula throws on junk, so every mark is checked
here first. Accepted formats (surrounding whitespace is ignored):

* a plain number: ``2.98``, ``13.45``, ``7`` (seconds, or metres for a throw or jump)
* ``m:ss.ss``: ``2:15.30`` (minutes, then seconds below 60, decimals optional)
* a comma may be used as the decimal separator: ``2,98``
* one trailing unit, ``m`` or ``s`` in either case, optionally after a space: ``2.98s``

Marks must be greater than zero. More than two decimals is allowed but warns.

On success ``normalised`` is the text to store: ``.`` as the separator and no
unit. The Grist formula turns that text into exactly ``value``.
"""

import math
import re
from dataclasses import dataclass

_PLAIN = re.compile(r"(?P<whole>[0-9]{1,6})(?:[.,](?P<frac>[0-9]+))?")
_MINUTES = re.compile(r"(?P<min>[0-9]{1,4}):(?P<sec>[0-9]{1,2})(?:[.,](?P<frac>[0-9]+))?")

MSG_EMPTY = "Enter a mark, for example 2.98 or 2:15.30."
MSG_INVALID = "Not a valid mark. Use a number like 2.98, or minutes like 2:15.30."
MSG_SECONDS = "Seconds must be below 60 in m:ss format, for example 2:15.30."
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
    if s[-1] in "msMS":
        s = s[:-1].rstrip()

    if m := _MINUTES.fullmatch(s):
        if int(m["sec"]) >= 60:
            return _fail(MSG_SECONDS)
        frac = m["frac"] or ""
        number = f"{int(m['min'])}:{m['sec']}" + (f".{frac}" if frac else "")
        value = int(m["min"]) * 60 + float(f"{m['sec']}.{frac or '0'}")
    elif m := _PLAIN.fullmatch(s):
        frac = m["frac"] or ""
        number = m["whole"] + (f".{frac}" if frac else "")
        value = float(number)
    else:
        return _fail(MSG_INVALID)

    if not math.isfinite(value):  # defensive; the patterns already bound the size
        return _fail(MSG_INVALID)
    if value <= 0:
        return _fail(MSG_ZERO)
    warning = MSG_DECIMALS if len(frac) > 2 else None
    return MarkResult(ok=True, value=value, normalised=number, warning=warning)
