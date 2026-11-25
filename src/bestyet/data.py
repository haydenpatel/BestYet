"""Read-only access to the BestYet tables, with Ref ids resolved to names.

Grist owns the logic: `Value`, `Date`, `IsPB` and `IsNewPB` on Results, `ResultCount` on Meets
and `Best`, `BestMeet` and `BestDate` on the PB summary table are read as Grist computed them,
never recalculated here. A cell Grist could not compute, or an empty one, reads as None.
"""

from dataclasses import dataclass
from datetime import date

from flask import current_app

from bestyet import dates, schema
from bestyet.grist import GristClient
from bestyet.lookups import Lookups


@dataclass(frozen=True)
class Athlete:
    id: int
    name: str
    grade: str | None
    division: str | None


@dataclass(frozen=True)
class Event:
    id: int
    name: str
    kind: str | None
    category: str | None


@dataclass(frozen=True)
class Meet:
    id: int
    name: str
    date: date | None
    location: str | None
    result_count: int


@dataclass(frozen=True)
class Result:
    id: int
    athlete_id: int | None
    athlete: str | None
    event_id: int | None
    event: str | None
    meet_id: int | None
    meet: str | None
    mark: str | None
    place: int | None
    grade: str | None
    division: str | None
    implement: str | None
    value: float | None
    date: date | None
    is_pb: bool
    is_new_pb: bool


@dataclass(frozen=True)
class PersonalBest:
    athlete_id: int | None
    athlete: str | None
    event_id: int | None
    event: str | None
    implement: str | None
    count: int
    best: float | None
    best_date: date | None
    best_meet_id: int | None
    best_meet: str | None


def _text(value: object) -> str | None:
    """A text cell trimmed as `Lookups` trims names, or None when it is blank or not text
    (such as a Grist error cell)."""
    return value.strip() or None if isinstance(value, str) else None


def _int(value: object) -> int | None:
    """An integer cell. Grist stores 0 in an empty Ref, and a list in an error cell."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class Data:
    """The tables the screens show. Every method reads from Grist when called."""

    def __init__(self, client: GristClient, lookups: Lookups | None = None):
        self._client = client
        self._lookups = lookups or Lookups(client)

    def athletes(self) -> list[Athlete]:
        return [
            Athlete(r["id"], name, _text(f.get("Grade")), _text(f.get("Division")))
            for r in self._client.records("Athletes")
            if (name := _text((f := r["fields"]).get("Name")))
        ]

    def events(self) -> list[Event]:
        """Events with their `Category` (None while it is empty)."""
        return [
            Event(r["id"], name, _text(f.get("Kind")), _text(f["Category"]))
            for r in self._client.records("Events")
            if (name := _text((f := r["fields"]).get("Name")))
        ]

    def meets(self) -> list[Meet]:
        return [
            Meet(
                r["id"],
                name,
                dates.from_grist(f.get("Date")),
                _text(f.get("Location")),
                _int(f.get("ResultCount")) or 0,
            )
            for r in self._client.records("Meets")
            if (name := _text((f := r["fields"]).get("Name")))
        ]

    def results(self) -> list[Result]:
        self._lookups.invalidate()  # names as they are now, not as cached up to 30 s ago
        out = []
        for r in self._client.records("Results"):
            f = r["fields"]
            athlete, event, meet = _int(f.get("Athlete")), _int(f.get("Event")), _int(f.get("Meet"))
            out.append(
                Result(
                    id=r["id"],
                    athlete_id=athlete,
                    athlete=athlete and self._lookups.athlete_name(athlete),
                    event_id=event,
                    event=event and self._lookups.event_name(event),
                    meet_id=meet,
                    meet=meet and self._lookups.meet_name(meet),
                    mark=_text(f.get("Mark")),
                    place=_int(f.get("Place")),
                    grade=_text(f.get("Grade")),
                    division=_text(f.get("Division")),
                    implement=_text(f.get("Implement")),
                    value=_number(f.get("Value")),
                    date=dates.from_grist(f.get("Date")),
                    is_pb=f.get("IsPB") is True,
                    is_new_pb=f.get("IsNewPB") is True,
                )
            )
        return out

    def personal_bests(self) -> list[PersonalBest]:
        """One row per athlete, event and implement, as Grist's summary table holds them."""
        self._lookups.invalidate()
        out = []
        for r in self._client.records(schema.SUMMARY.id):
            f = r["fields"]
            athlete, event = _int(f.get("Athlete")), _int(f.get("Event"))
            meet = _int(f.get("BestMeet"))
            out.append(
                PersonalBest(
                    athlete_id=athlete,
                    athlete=athlete and self._lookups.athlete_name(athlete),
                    event_id=event,
                    event=event and self._lookups.event_name(event),
                    implement=_text(f.get("Implement")),
                    count=_int(f.get("count")) or 0,
                    best=_number(f.get("Best")),
                    best_date=dates.from_grist(f.get("BestDate")),
                    best_meet_id=meet,
                    best_meet=meet and self._lookups.meet_name(meet),
                )
            )
        return out


def current_data() -> Data:
    """The data layer of the running app."""
    return current_app.extensions["bestyet_data"]
