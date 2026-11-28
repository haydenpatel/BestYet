"""One athlete's personal bests, grouped by event category.

The rows are the PB summary table's, as Grist holds them: nothing is calculated here. A row is
new when Grist marks a result with its athlete, event and implement `IsNewPB`.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from bestyet.data import Event, PersonalBest, Result

OTHER = "Other"  # the group for a blank category


@dataclass(frozen=True)
class PbRow:
    pb: PersonalBest
    is_new: bool


def by_category(
    athlete_id: int,
    bests: Iterable[PersonalBest],
    events: Iterable[Event],
    results: Iterable[Result],
) -> dict[str, list[PbRow]]:
    """The athlete's rows under each category name. Neither the categories nor the rows are in
    a meaningful order."""
    categories = {e.id: e.category for e in events}
    new = {(r.event_id, r.implement) for r in results if r.athlete_id == athlete_id and r.is_new_pb}
    groups: dict[str, list[PbRow]] = {}
    for pb in bests:
        if pb.athlete_id != athlete_id:
            continue
        row = PbRow(pb, (pb.event_id, pb.implement) in new)
        groups.setdefault(categories.get(pb.event_id) or OTHER, []).append(row)
    return groups
