"""The Grist schema contract from SPEC.md, as plain data.

The init script, ``--check`` and the app's startup check all read this module,
so there is one definition of the schema. It has no side effects and makes no
Grist calls.

The trigger columns, formulas and recalc settings below are the contract, not
decoration: the app relies on Grist to compute ``Value``, ``Date``, ``IsPB`` and
the ``Grade`` / ``Implement`` defaults. Table and column ids are case sensitive.
Formula text is stored verbatim from SPEC.md ("Reference formulas"); a test
keeps the two in sync. Do not name a variable ``rec`` in a formula (Grist
reserves it).

``recalc_when`` and ``recalc_deps`` mirror Grist's column fields of the same
meaning: ``recalc_when=0`` is "on new records or when a dependency changes",
and ``recalc_deps`` lists the ids of the columns that trigger it.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    """One column. ``recalc_deps`` holds column ids; Grist's column API stores column
    refs, so the init script must map ids to refs when it sends ``recalcDeps``.
    ``visible_col`` is likewise an id, mapped to a ref for Grist's ``visibleCol``."""

    id: str
    type: str  # Grist type: Text, Choice, Date, Int, Numeric, Bool or Ref:<Table>
    choices: tuple[str, ...] | None = None  # None: choices are deployment data
    ref_table: str | None = None
    formula: str | None = None
    is_formula: bool = False  # True: regular formula column; False with a formula: trigger
    recalc_when: int | None = None
    recalc_deps: tuple[str, ...] | None = None
    visible_col: str | None = None  # Ref columns: id of the target column shown (its name)


@dataclass(frozen=True)
class Table:
    id: str
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class Summary:
    """A summary table, described as data only. Creating it is the init script's job.
    Grist keeps a summary table only while a view section uses it, so the script creates it
    through a view section on ``page``."""

    id: str
    source_table: str
    group_by: tuple[str, ...]
    columns: tuple[Column, ...]  # beyond the group-by columns and Grist's own ``count``
    page: str = "Personal Bests"  # the page whose view section keeps the table alive


# Formulas, verbatim from SPEC.md.
VALUE_FORMULA = """\
s = str($Mark).strip().lower().rstrip("ms").replace(",", ".")
if not s:
  return None
if ":" in s:
  m, sec = s.split(":")
  return int(m) * 60 + float(sec)
return float(s)"""

IS_PB_FORMULA = """\
if $Value is None or not $Event:
  return False
vals = [r.Value for r in Results.lookupRecords(Athlete=$Athlete, Event=$Event, Implement=$Implement) if r.Value is not None]
best = min(vals) if $Event.Kind == "Time" else max(vals)
return $Value == best"""

DATE_FORMULA = "$Meet.Date"

GRADE_FORMULA = "$Athlete.Grade"

IMPLEMENT_FORMULA = """\
g = $Grade or $Athlete.Grade
match = Implements.lookupOne(Event=$Event, Grade=g)
return match.Weight if match else \"\""""

BEST_FORMULA = """\
vals = [r.Value for r in $group if r.Value is not None]
if not vals:
  return None
return min(vals) if $Event.Kind == "Time" else max(vals)"""

EVENT_KINDS = ("Time", "Distance")

# Choices are deployment data: the contract only requires Grade to be a Choice.
# The init script uses these when it creates the column.
DEFAULT_GRADE_CHOICES = tuple(f"Grade {n}" for n in range(6, 13))

_TRIGGER_NEW_OR_DEP = 0

TABLES = (
    Table(
        "Athletes",
        (
            Column("Name", "Text"),
            Column("Grade", "Choice"),
        ),
    ),
    Table(
        "Events",
        (
            Column("Name", "Text"),
            Column("Kind", "Choice", choices=EVENT_KINDS),
        ),
    ),
    Table(
        "Meets",
        (
            Column("Name", "Text"),
            Column("Date", "Date"),
            Column("Location", "Text"),
        ),
    ),
    Table(
        "Implements",
        (
            Column("Event", "Ref:Events", ref_table="Events", visible_col="Name"),
            Column("Grade", "Choice"),
            Column("Weight", "Text"),
        ),
    ),
    Table(
        "Results",
        (
            Column("Athlete", "Ref:Athletes", ref_table="Athletes", visible_col="Name"),
            Column("Event", "Ref:Events", ref_table="Events", visible_col="Name"),
            Column("Meet", "Ref:Meets", ref_table="Meets", visible_col="Name"),
            Column("Mark", "Text"),
            Column("Place", "Int"),
            Column(
                "Grade",
                "Choice",
                formula=GRADE_FORMULA,
                recalc_when=_TRIGGER_NEW_OR_DEP,
                recalc_deps=("Athlete",),
            ),
            Column(
                "Implement",
                "Text",
                formula=IMPLEMENT_FORMULA,
                recalc_when=_TRIGGER_NEW_OR_DEP,
                recalc_deps=("Event",),
            ),
            Column("Value", "Numeric", formula=VALUE_FORMULA, is_formula=True),
            Column("Date", "Date", formula=DATE_FORMULA, is_formula=True),
            Column("IsPB", "Bool", formula=IS_PB_FORMULA, is_formula=True),
        ),
    ),
)

SUMMARY = Summary(
    id="Results_summary_Athlete_Event_Implement",
    source_table="Results",
    group_by=("Athlete", "Event", "Implement"),
    columns=(Column("Best", "Numeric", formula=BEST_FORMULA, is_formula=True),),
    page="Personal Bests",
)
# Grist adds ``count`` (Int) to every summary table; the contract requires it.
SUMMARY_REQUIRED_COLUMNS = ("count", *(c.id for c in SUMMARY.columns))

# Results columns the app may write. Grade and Implement are normally left out
# of the payload so their trigger formulas fill them in.
WRITABLE_RESULTS_COLUMNS = ("Athlete", "Event", "Meet", "Mark", "Place", "Grade", "Implement")
# Computed by Grist, read only: never write these.
COMPUTED_RESULTS_COLUMNS = ("Value", "Date", "IsPB")

# Example Events seed from SPEC.md: (name, kind).
EXAMPLE_EVENTS = (
    ("60m", "Time"),
    ("80m", "Time"),
    ("100m", "Time"),
    ("200m", "Time"),
    ("400m", "Time"),
    ("800m", "Time"),
    ("1500m", "Time"),
    ("3000m", "Time"),
    ("80m Hurdles", "Time"),
    ("100m Hurdles", "Time"),
    ("Long Jump", "Distance"),
    ("High Jump", "Distance"),
    ("Triple Jump", "Distance"),
    ("Shot Put", "Distance"),
    ("Discus", "Distance"),
    ("Javelin", "Distance"),
    ("Turbo Javelin", "Distance"),
    ("Vortex", "Distance"),
)

# Example Implements rows (optional seed): (event name, grade, weight).
EXAMPLE_IMPLEMENTS = (
    ("Discus", "Grade 8", "500g"),
    ("Discus", "Grade 9", "500g"),
    ("Shot Put", "Grade 8", "1.5kg"),
    ("Shot Put", "Grade 9", "1.5kg"),
)

# Demo data (init script `--demo-data`): obviously fictional. Events are named as in
# EXAMPLE_EVENTS and the Discus grades have rows in EXAMPLE_IMPLEMENTS, so the demo rows
# resolve their Implement. Dates are ISO text.
DEMO_ATHLETES = (  # (name, grade)
    ("Athlete A", "Grade 8"),
    ("Athlete B", "Grade 9"),
    ("Athlete C", "Grade 9"),
)
DEMO_MEETS = (  # (name, ISO date, location)
    ("Demo Meet 1", "2026-03-07", "Demo Field"),
    ("Demo Meet 2", "2026-04-18", "Demo Stadium"),
)
# (athlete, event, meet, mark, place). Two results for the same athlete, event and implement
# show a personal best; Discus is a throwing event with an implement.
DEMO_RESULTS = (
    ("Athlete A", "100m", "Demo Meet 1", "13.45", 2),
    ("Athlete A", "100m", "Demo Meet 2", "13.20", 1),
    ("Athlete A", "800m", "Demo Meet 2", "2:15.30", 3),
    ("Athlete B", "Discus", "Demo Meet 1", "21.50", 2),
    ("Athlete B", "Discus", "Demo Meet 2", "23.10", 1),
    ("Athlete C", "Long Jump", "Demo Meet 1", "4.52", 1),
    ("Athlete C", "100m", "Demo Meet 2", "14.02", 4),
)


def table(name: str) -> Table:
    """Return the table with this (case sensitive) id; raise KeyError if unknown."""
    for t in TABLES:
        if t.id == name:
            return t
    raise KeyError(name)


def required_columns(table_id: str) -> tuple[str, ...]:
    """Ids of the columns a document must have in this table."""
    return tuple(c.id for c in table(table_id).columns)
