"""The schema module must match SPEC.md and be internally consistent."""

import re
from pathlib import Path

import pytest

from bestyet import schema

SPEC = (Path(__file__).resolve().parent.parent / "SPEC.md").read_text()


def spec_formula(label: str) -> str:
    """The formula in SPEC.md under a heading line that starts with this label.

    Handles a fenced block on the following lines, or an inline `code` after the colon.
    """
    block = re.search(
        rf"^`{re.escape(label)}`[^\n]*\n```python\n(.*?)\n```", SPEC, re.DOTALL | re.MULTILINE
    )
    if block:
        return block.group(1)
    inline = re.search(rf"^`{re.escape(label)}`[^\n]*: `([^`\n]+)`", SPEC, re.MULTILINE)
    assert inline, f"no formula for {label} in SPEC.md"
    return inline.group(1)


def results_col(name: str) -> schema.Column:
    return next(c for c in schema.table("Results").columns if c.id == name)


@pytest.mark.parametrize(
    ("label", "column"),
    [
        ("Results.Value", results_col("Value")),
        ("Results.IsPB", results_col("IsPB")),
        ("Results.Date", results_col("Date")),
        ("Results.Grade", results_col("Grade")),
        ("Results.Division", results_col("Division")),
        ("Results.Implement", results_col("Implement")),
        ("Results_summary_Athlete_Event_Implement.Best", schema.SUMMARY.columns[0]),
    ],
)
def test_formula_matches_spec(label, column):
    assert column.formula == spec_formula(label)


def test_formula_kinds():
    for name in ("Value", "Date", "IsPB"):
        assert results_col(name).is_formula
    for name, deps in (
        ("Grade", ("Athlete",)),
        ("Division", ("Athlete",)),
        ("Implement", ("Event", "Division")),
    ):
        col = results_col(name)
        assert not col.is_formula
        assert col.recalc_deps == deps
        assert col.recalc_when == 0


def test_spec_tables_exist():
    named = re.findall(r"^### (\w+)", SPEC.split("## Grist data model")[1], re.MULTILINE)
    spec_tables = [
        n for n in named if n in {"Athletes", "Events", "Meets", "Implements", "Results"}
    ]
    assert sorted(spec_tables) == sorted(t.id for t in schema.TABLES)


def test_spec_columns_present():
    section = SPEC.split("## Grist data model")[1].split("### Trigger formulas")[0]
    for t in schema.TABLES:
        body = re.split(rf"^### {t.id}\b.*$", section, flags=re.MULTILINE)[1].split("\n### ")[0]
        for col in t.columns:
            assert f"`{col.id}`" in body, f"{t.id}.{col.id} not in SPEC"


def test_ids_unique():
    assert len({t.id for t in schema.TABLES}) == len(schema.TABLES)
    for t in schema.TABLES:
        ids = [c.id for c in t.columns]
        assert len(ids) == len(set(ids)), t.id


def test_refs_point_at_defined_tables():
    ids = {t.id for t in schema.TABLES}
    for t in schema.TABLES:
        for c in t.columns:
            if c.type.startswith("Ref:"):
                assert c.ref_table == c.type.removeprefix("Ref:")
                assert c.ref_table in ids
            else:
                assert c.ref_table is None


def test_recalc_deps_are_columns():
    for t in schema.TABLES:
        ids = {c.id for c in t.columns}
        for c in t.columns:
            assert set(c.recalc_deps or ()) <= ids


def test_summary_spec():
    s = schema.SUMMARY
    assert s.id == "Results_summary_Athlete_Event_Implement"
    assert s.group_by == ("Athlete", "Event", "Implement")
    assert set(s.group_by) <= set(schema.required_columns(s.source_table))
    assert schema.SUMMARY_REQUIRED_COLUMNS == ("count", *(c.id for c in s.columns))
    assert "Best" in schema.SUMMARY_REQUIRED_COLUMNS


def test_events_seed():
    names = [n for n, _ in schema.EXAMPLE_EVENTS]
    assert len(names) == len(set(names))
    assert {k for _, k in schema.EXAMPLE_EVENTS} <= set(schema.EVENT_KINDS)
    seed_line = re.search(r"Example seed: (.+)\.", SPEC).group(1)
    assert names == seed_line.split(", ")
    assert schema.table("Events").columns[1].choices == ("Time", "Distance")


def test_implements_seed_references_seed_events():
    names = {n for n, _ in schema.EXAMPLE_EVENTS}
    for event, grade, division, weight in schema.SAMPLE_IMPLEMENTS:
        assert event in names
        assert grade and division and weight


def test_sample_implements_cover_each_grade_and_division_once():
    rows = schema.SAMPLE_IMPLEMENTS
    assert len(rows) == 36
    assert len({r[:3] for r in rows}) == 36  # no duplicate Event, Grade and Division
    assert {r[0] for r in rows} == {"Discus", "Shot Put"}
    assert {r[1] for r in rows} == {f"Grade {n}" for n in range(7, 16)}
    assert {r[2] for r in rows} == {"Girls", "Boys"}  # no Open rows
    assert {r[1] for r in rows} <= set(schema.DEFAULT_GRADE_CHOICES)
    assert {r[2] for r in rows} <= set(schema.DEFAULT_DIVISION_CHOICES)
    weight = {r[:3]: r[3] for r in rows}
    assert weight["Discus", "Grade 12", "Girls"] == "750g"
    assert weight["Discus", "Grade 12", "Boys"] == "1.000kg"


def test_sample_implement_weights_are_pinned():
    discus = {
        7: ("500g", "500g"),
        8: ("500g", "500g"),
        9: ("500g", "500g"),
        10: ("750g", "750g"),
        11: ("750g", "750g"),
        12: ("750g", "1.000kg"),
        13: ("1.000kg", "1.000kg"),
        14: ("1.000kg", "1.250kg"),
        15: ("1.000kg", "1.250kg"),
    }
    shot = {
        7: ("1.000kg", "1.000kg"),
        8: ("1.500kg", "1.500kg"),
        9: ("1.500kg", "1.500kg"),
        10: ("2.000kg", "2.000kg"),
        11: ("2.000kg", "3.000kg"),
        12: ("3.000kg", "3.000kg"),
        13: ("3.000kg", "4.000kg"),
        14: ("3.000kg", "5.000kg"),
        15: ("3.000kg", "5.000kg"),
    }
    want = {
        (event, f"Grade {g}", div): w
        for event, table in (("Discus", discus), ("Shot Put", shot))
        for g, pair in table.items()
        for div, w in zip(("Girls", "Boys"), pair, strict=True)
    }
    assert {r[:3]: r[3] for r in schema.SAMPLE_IMPLEMENTS} == want


def test_division_columns_and_label():
    for table in ("Athletes", "Implements", "Results"):
        col = next(c for c in schema.table(table).columns if c.id == "Division")
        assert col.type == "Choice"
        assert col.choices is None  # deployment data
    athletes = next(c for c in schema.table("Athletes").columns if c.id == "Division")
    assert athletes.label == "Default division"
    assert schema.DEFAULT_DIVISION_CHOICES == ("Girls", "Boys", "Open")


def test_writable_and_computed_split():
    results = set(schema.required_columns("Results"))
    writable = set(schema.WRITABLE_RESULTS_COLUMNS)
    computed = set(schema.COMPUTED_RESULTS_COLUMNS)
    assert not writable & computed
    assert "Division" not in writable  # recomputed when Athlete is set on create
    assert writable | computed | {"Division"} == results
    for name in computed:
        assert results_col(name).is_formula


def test_default_grade_choices():
    assert schema.DEFAULT_GRADE_CHOICES[0] == "Grade 6"
    assert schema.DEFAULT_GRADE_CHOICES[-1] == "Grade 15"
    assert {f"Grade {n}" for n in range(7, 16)} <= set(schema.DEFAULT_GRADE_CHOICES)


def test_rec_not_used_in_formulas():
    formulas = [c.formula for t in schema.TABLES for c in t.columns if c.formula]
    formulas += [c.formula for c in schema.SUMMARY.columns if c.formula]
    for f in formulas:
        assert not re.search(r"\brec\b", f)


def test_helpers():
    assert schema.table("Results").id == "Results"
    assert schema.required_columns("Meets") == ("Name", "Date", "Location")
    with pytest.raises(KeyError):
        schema.table("results")
