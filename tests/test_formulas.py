"""Run the Grist formulas that pick PB details against stand-in records.

Grist evaluates the real formulas; this checks their logic, with `$name` read from a record
and `Results` standing in for the Results table.
"""

import re
from types import SimpleNamespace as NS

from bestyet import schema


class Table:
    def __init__(self, rows):
        self.all = rows

    def lookupRecords(self, **match):
        return [r for r in self.all if all(getattr(r, k) == v for k, v in match.items())]


def result(date, value=10.0, meet=None, athlete=1, event=1, implement=""):
    return NS(Date=date, Value=value, Meet=meet, Athlete=athlete, Event=event, Implement=implement)


def run(formula, env=None, **names):
    """Evaluate a formula body with `$x` bound to names[x]."""
    body = re.sub(r"\$(\w+)", r"S_\1", formula)
    code = "def f():\n" + "".join(f"  {line}\n" for line in body.splitlines())
    scope = {**(env or {}), **{"S_" + k: v for k, v in names.items()}}
    exec(code, scope)  # noqa: S102 (our own formulas)
    return scope["f"]()


def is_new_pb(this, results, is_pb=True):
    return run(
        schema.IS_NEW_PB_FORMULA,
        IsPB=is_pb,
        Date=this.Date,
        Athlete=this.Athlete,
        Event=this.Event,
        Implement=this.Implement,
        Value=this.Value,
        env={"Results": Table(results)},
    )


def best_meet(group, best):
    return run(schema.BEST_MEET_FORMULA, Best=best, group=group)


def test_an_empty_later_meet_does_not_hide_the_new_pb():
    # Results.Date comes from the meet, so a meet with no results never appears in Results.all.
    old, new = result(100), result(200, value=9.0)
    assert is_new_pb(new, [old, new]) is True
    assert is_new_pb(old, [old, new], is_pb=False) is False


def test_a_result_that_only_ties_an_earlier_pb_is_not_new():
    first, tie = result(100), result(200)
    assert is_new_pb(first, [first, tie]) is False  # not the latest meet
    assert is_new_pb(tie, [first, tie]) is False  # latest meet, but the PB was earlier


def test_a_tie_within_the_latest_meet_date_counts():
    a, b = result(200), result(200)
    assert is_new_pb(a, [a, b]) and is_new_pb(b, [a, b])


def test_best_meet_is_the_earliest_tied_result():
    later, earlier = result(200, meet="M2"), result(100, meet="M1")
    assert best_meet([later, earlier], 10.0) == "M1"


def test_best_meet_keeps_a_best_result_in_an_undated_meet():
    undated, worse = result(None, value=9.0, meet="M0"), result(100, value=12.0, meet="M1")
    assert best_meet([worse, undated], 9.0) == "M0"
    assert best_meet([], None) is None
