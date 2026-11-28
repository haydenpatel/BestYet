"""One athlete's personal bests, grouped by event category, in the order the screen shows them.

The rows are the PB summary table's, as Grist holds them: nothing is calculated here. A row is
new when Grist marks a result with its athlete, event and exact implement text `IsNewPB`. A row with no
event or no best is left out.

Categories follow the order of the `Events.Category` choices, then any category that is not a
choice, by name, then Other. Events with a number in their name come first, by that number
(80m, 100m), then the rest by name. One event's implements are newest best first.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from bestyet.data import Event, PersonalBest, Result

OTHER = "Other"  # the group for a blank category
_NUMBER = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class PbRow:
    pb: PersonalBest
    kind: str | None  # the event's Kind: "Time" or "Distance"
    is_new: bool


def _row_order(row: PbRow):
    name = row.pb.event.casefold()
    number = _NUMBER.search(name)
    newest = -(row.pb.best_date or date.min).toordinal()  # an undated best goes last
    return (
        (0, int(number[0])) if number else (1, 0),
        name,
        row.pb.event_id,
        newest,
        row.pb.implement or "",
    )


def by_category(
    athlete_id: int,
    bests: Iterable[PersonalBest],
    events: Iterable[Event],
    results: Iterable[Result],
    choices: Sequence[str] = (),
) -> dict[str, list[PbRow]]:
    """The athlete's rows under each category name, both in display order. `choices` is the
    `Events.Category` choices in Grist's order."""
    by_id = {e.id: e for e in events}
    new = {
        (r.event_id, r.implement_key) for r in results if r.athlete_id == athlete_id and r.is_new_pb
    }
    groups: dict[str, list[PbRow]] = {}
    for pb in bests:
        if pb.athlete_id != athlete_id or pb.event is None or pb.best is None:
            continue
        event = by_id.get(pb.event_id)
        category = (event and event.category) or OTHER
        row = PbRow(pb, event and event.kind, (pb.event_id, pb.implement_key) in new)
        groups.setdefault(category, []).append(row)
    order = {name: i for i, name in enumerate(choices)}

    def category_order(name: str):
        return (name == OTHER, order.get(name, len(order)), name.casefold())

    return {
        name: sorted(groups[name], key=_row_order) for name in sorted(groups, key=category_order)
    }
