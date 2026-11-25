"""What the init script writes to Grist: the steps, seeds, demo data and the default table."""

import json

import pytest
from fake_grist import BASE, DOC, FakeGrist
from init_helpers import (
    CREATE,
    DEFAULTS,
    ENV,
    ENV_NO_DOC,
    before_nth_get,
    event_names,
    full_fake,
    full_tables,
    incomplete_fake,
    run,
)

from bestyet import dates, marks, schema
from bestyet.config import Config
from bestyet.grist import GristClient
from bestyet.init_plan import implement_event_names
from bestyet.init_steps import AddColumns, CreateTable


@pytest.fixture
def fresh():
    """A fake Grist after a plain run against an empty document."""
    fake = FakeGrist()
    assert run(fake) == 0
    return fake


def removals(fake):
    return [a for a in fake.actions if a[0] == "RemoveTable"]


# The contract.


def test_fresh_document_gets_the_whole_contract_and_no_example_rows(fresh):
    assert set(fresh.tables) == {t.id for t in schema.TABLES} | {schema.SUMMARY.id}
    for t in schema.TABLES:
        assert set(fresh.tables[t.id]) == {c.id for c in t.columns}
    assert fresh.tables["Results"]["Athlete"]["type"] == "Ref:Athletes"
    assert fresh.tables["Meets"]["Date"] == {"type": "Date"}
    assert not fresh.rows.get("Events") and not fresh.rows.get("Implements")


def test_formula_and_trigger_columns_carry_their_settings(fresh):
    results = fresh.tables["Results"]
    columns = {c.id: c for c in schema.table("Results").columns}
    for name in ("Value", "Date", "IsPB"):
        assert (
            results[name]["isFormula"] is True and results[name]["formula"] == columns[name].formula
        )
    triggers = {
        "Grade": (schema.GRADE_FORMULA, ["Athlete"]),
        "Division": (schema.DIVISION_FORMULA, ["Athlete"]),
        # Implement depends on a column created in the same step as itself.
        "Implement": (schema.IMPLEMENT_FORMULA, ["Event", "Division"]),
    }
    for name, (formula, deps) in triggers.items():
        col = results[name]
        assert col["isFormula"] is False and col["formula"] == formula
        # Fire on new records or a dependency change, naming the columns by ref.
        assert col["recalcWhen"] == 0
        assert col["recalcDeps"] == ["L", *(fresh.refs[("Results", d)] for d in deps)]


def test_summary_table_is_created_through_a_page_and_has_best(fresh):
    assert fresh.summary_tables == {schema.SUMMARY.id}
    assert list(fresh.views.values()) == ["Personal Bests"]
    [create] = [a for a in fresh.actions if a[0] == "CreateViewSection"]
    group = [fresh.refs[("Results", g)] for g in schema.SUMMARY.group_by]
    assert create[1] == fresh.refs[("Results", "")] and create[2] == 0 and create[4] == group
    best = fresh.tables[schema.SUMMARY.id]["Best"]
    assert best["isFormula"] is True and best["formula"] == schema.BEST_FORMULA


def test_pb_details_are_grist_formula_columns(fresh):
    for table, name, formula in [
        ("Meets", "ResultCount", schema.RESULT_COUNT_FORMULA),
        ("Results", "IsNewPB", schema.IS_NEW_PB_FORMULA),
        (schema.SUMMARY.id, "BestMeet", schema.BEST_MEET_FORMULA),
        (schema.SUMMARY.id, "BestDate", schema.BEST_DATE_FORMULA),
    ]:
        col = fresh.tables[table][name]
        assert col["isFormula"] is True and col["formula"] == formula
    best_meet = fresh.tables[schema.SUMMARY.id]["BestMeet"]
    assert best_meet["type"] == "Ref:Meets"
    assert best_meet["visibleCol"] == fresh.refs[("Meets", "Name")]
    best_ref = fresh.refs[(schema.SUMMARY.id, "BestMeet")]
    assert fresh.display[(schema.SUMMARY.id, best_ref)] == "$BestMeet.Name"


def test_an_older_document_gets_the_pb_detail_columns_and_a_rerun_changes_nothing():
    fake = full_fake()
    for table, column in [
        ("Meets", "ResultCount"),
        ("Results", "IsNewPB"),
        (schema.SUMMARY.id, "BestMeet"),
        (schema.SUMMARY.id, "BestDate"),
    ]:
        del fake.tables[table][column]
    assert run(fake) == 0
    assert fake.tables["Meets"]["ResultCount"]["formula"] == schema.RESULT_COUNT_FORMULA
    assert fake.tables["Results"]["IsNewPB"]["formula"] == schema.IS_NEW_PB_FORMULA
    summary = fake.tables[schema.SUMMARY.id]
    assert summary["BestDate"]["formula"] == schema.BEST_DATE_FORMULA
    assert summary["BestMeet"]["visibleCol"] == fake.refs[("Meets", "Name")]
    before = len(fake.writes)
    assert run(fake) == 0
    assert len(fake.writes) == before


def test_ref_columns_show_names(fresh):
    for table, col, target in [
        ("Results", "Athlete", "Athletes"),
        ("Results", "Event", "Events"),
        ("Results", "Meet", "Meets"),
        ("Implements", "Event", "Events"),
    ]:
        assert fresh.tables[table][col]["visibleCol"] == fresh.refs[(target, "Name")]
    athlete = fresh.refs[("Results", "Athlete")]
    assert fresh.display[("Results", athlete)] == "$Athlete.Name"  # not just visibleCol
    summary = fresh.tables[schema.SUMMARY.id]
    assert summary["Athlete"]["visibleCol"] == fresh.refs[("Athletes", "Name")]
    assert summary["Event"]["visibleCol"] == fresh.refs[("Events", "Name")]


def test_choices_and_the_default_division_label(fresh):
    kind = json.loads(fresh.tables["Events"]["Kind"]["widgetOptions"])
    assert kind == {"choices": ["Time", "Distance"]}
    category = json.loads(fresh.tables["Events"]["Category"]["widgetOptions"])
    assert category == {"choices": list(schema.EVENT_CATEGORIES)}
    grade = json.loads(fresh.tables["Athletes"]["Grade"]["widgetOptions"])
    assert grade["choices"] == list(schema.DEFAULT_GRADE_CHOICES)
    assert fresh.tables["Athletes"]["Division"]["label"] == "Default division"
    assert "label" not in fresh.tables["Results"]["Division"]
    assert "label" not in fresh.tables["Implements"]["Division"]


# Re-runs only add what is missing and never change what the owner has.


def test_second_run_writes_nothing(fresh):
    fresh.writes.clear()
    assert run(fresh) == 0
    assert fresh.writes == []


def test_complete_document_is_left_alone():
    assert run(fake := full_fake()) == 0
    assert fake.writes == []


def test_partial_document_gets_only_the_missing_parts():
    tables = full_tables()
    del tables["Meets"]
    del tables["Results"]["Mark"]
    tables["Athletes"]["Name"] = {"type": "Numeric"}  # wrong type: left as is
    fake = full_fake(tables)
    assert run(fake) == 0
    assert set(fake.tables["Meets"]) == {"Name", "Date", "Location", "ResultCount"}
    assert fake.tables["Results"]["Mark"] == {"type": "Text"}
    assert fake.tables["Athletes"]["Name"] == {"type": "Numeric"}
    assert len([w for w in fake.writes if w[0] == "POST"]) == 3


def test_visible_col_is_set_only_where_unset():
    tables = full_tables()
    tables["Results"]["Athlete"]["visibleCol"] = 77  # the owner's choice
    del tables["Results"]["Event"]["visibleCol"]
    fake = full_fake(tables)
    assert run(fake) == 0
    assert fake.tables["Results"]["Athlete"]["visibleCol"] == 77
    assert fake.tables["Results"]["Event"]["visibleCol"] == fake.refs[("Events", "Name")]


@pytest.mark.parametrize("name", ["Personal bests", "personal bests", "  PERSONAL BESTS "])
def test_a_page_with_another_case_is_kept_not_renamed_or_duplicated(name):
    fake = full_fake()
    fake.views = {1: name}
    assert run(fake) == 0
    assert fake.writes == [] and list(fake.views.values()) == [name]


def test_missing_page_is_created_for_an_existing_summary_table():
    fake = full_fake()
    fake.views.clear()
    assert run(fake) == 0
    assert list(fake.views.values()) == ["Personal Bests"]
    assert len([a for a in fake.actions if a[0] == "CreateViewSection"]) == 1


def test_unrelated_tables_are_refused_then_left_alone_when_allowed(capsys):
    fake = FakeGrist({"Notes": {"Text": {"type": "Text"}}})
    fake.tables["Notes_summary_X"] = {}
    fake.summary_tables.add("Notes_summary_X")
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert "Notes" in err and "Notes_summary_X" not in err  # summary tables are not unrelated
    assert fake.writes == []
    fake.fail_on = "tables/Notes/columns"  # their columns are never read
    assert run(fake, "--allow-existing-tables") == 0
    assert "Athletes" in fake.tables and fake.tables["Notes"] == {"Text": {"type": "Text"}}


def test_client_add_methods_hit_the_expected_endpoints():
    fake = FakeGrist({"T": {}})
    key = Config.from_env(ENV).grist_api_key
    with GristClient(BASE, DOC, key, transport=fake.transport) as c:
        c.add_tables([{"id": "U", "columns": [{"id": "A", "fields": {"type": "Text"}}]}])
        c.add_columns("T", [{"id": "B", "fields": {"type": "Int"}}])
    assert fake.tables["U"] == {"A": {"type": "Text"}}
    assert fake.tables["T"] == {"B": {"type": "Int"}}


def test_renamed_ids_stop_with_an_error():
    fake = FakeGrist({"Athletes": {"Name": {"type": "Text"}}})
    client = GristClient(BASE, DOC, Config.from_env(ENV).grist_api_key, transport=fake.transport)
    col = schema.table("Athletes").columns[0]
    with pytest.raises(Exception, match="Name2"):
        AddColumns("Athletes", (col,)).apply(client)
    with pytest.raises(Exception, match="Athletes2"):
        CreateTable(schema.table("Athletes"), (col,)).apply(client)


# Seeds: default events and sample implements.


def test_seed_events_adds_only_the_events():
    fake = FakeGrist()
    assert run(fake, "--seed-events") == 0
    rows = [
        (r["fields"]["Name"], r["fields"]["Kind"], r["fields"]["Category"])
        for r in fake.rows["Events"]
    ]
    assert rows == list(schema.EXAMPLE_EVENTS)
    assert "Implements" not in fake.rows


def test_seeded_events_get_their_category_and_existing_ones_keep_theirs():
    fake = full_fake()
    fake.rows["Events"] = [
        {"id": 1, "fields": {"Name": "Discus", "Kind": "Distance", "Category": "Jumps"}},
        {"id": 2, "fields": {"Name": "100m", "Kind": "Time"}},  # blank: left blank
    ]
    assert run(fake, "--seed-events") == 0
    by_name = {r["fields"]["Name"]: r["fields"].get("Category") for r in fake.rows["Events"]}
    assert by_name["Discus"] == "Jumps" and by_name["100m"] is None
    assert by_name["Shot Put"] == "Throws" and by_name["800m"] == "Middle distance"
    for sample in (("--seed-implements",), ("--demo-data",)):  # these add their events with one too
        fake = FakeGrist()
        assert run(fake, *sample) == 0
        assert all(r["fields"]["Category"] for r in fake.rows["Events"])


def test_missing_category_column_is_added_and_a_rerun_changes_nothing():
    fake = incomplete_fake(table="Events", column="Category")
    assert run(fake) == 0
    assert fake.tables["Events"]["Category"]["type"] == "Choice"
    assert run(fake) == 0
    assert [w for w in fake.writes if "columns" in w[1]] == [
        ("POST", f"/api/docs/{DOC}/tables/Events/columns")
    ]


def test_seed_implements_adds_the_events_it_uses_and_resolves_their_ids():
    fake = FakeGrist()
    assert run(fake, "--seed-implements") == 0
    assert set(event_names(fake)) == set(implement_event_names())  # only the events it uses
    ids = {r["fields"]["Name"]: r["id"] for r in fake.rows["Events"]}
    got = [
        tuple(r["fields"][k] for k in ("Event", "Grade", "Division", "Weight"))
        for r in fake.rows["Implements"]
    ]
    assert got == [(ids[e], g, d, w) for e, g, d, w in schema.SAMPLE_IMPLEMENTS]


def test_seeding_twice_changes_nothing():
    fake = FakeGrist()
    assert run(fake, "--seed-implements") == 0
    rows, fake.writes[:] = json.dumps(fake.rows), []
    assert run(fake, "--seed-implements") == 0
    assert fake.writes == [] and json.dumps(fake.rows) == rows


def test_seed_implements_adds_only_the_missing_events_and_implements():
    fake = full_fake()
    fake.rows["Events"] = [r for r in fake.rows["Events"] if r["fields"]["Name"] != "Discus"]
    before = len(fake.rows["Events"])
    assert run(fake, "--seed-implements") == 0
    assert len(fake.rows["Events"]) == before + 1 and event_names(fake).count("Discus") == 1
    assert len(fake.rows["Implements"]) == len(schema.SAMPLE_IMPLEMENTS)
    # With every event present only the implements are written.
    fake = full_fake()
    assert run(fake, "--seed-implements") == 0
    assert fake.writes == [("POST", f"/api/docs/{DOC}/tables/Implements/records")]


def test_without_a_seed_flag_nothing_is_seeded():
    fake = full_fake()
    fake.rows["Events"] = []
    assert run(fake) == 0
    assert fake.writes == []


def test_existing_rows_are_kept_and_not_duplicated():
    fake = full_fake()
    fake.rows["Events"] = [
        {"id": 7, "fields": {"Name": "Discus", "Kind": "Time"}},  # differs: left alone
        {"id": 9, "fields": {"Name": "My Event", "Kind": "Time"}},
    ]
    owner = [
        {
            "id": 1,
            "fields": {"Event": 7, "Grade": "Grade 8", "Division": "Girls", "Weight": "999g"},
        },
        {"id": 2, "fields": {"Event": 9, "Grade": "Grade 9", "Division": "Boys", "Weight": "1kg"}},
    ]
    fake.rows["Implements"] = list(owner)
    assert run(fake, "--seed-implements") == 0
    assert event_names(fake) == ["Discus", "My Event", "Shot Put"]  # only the one it needs
    assert fake.rows["Events"][0]["fields"]["Kind"] == "Time"
    assert fake.rows["Implements"][:2] == owner
    # Event, Grade and Division identify a row: Discus Grade 8 Girls is there, Boys is not.
    keys = {
        (r["fields"]["Event"], r["fields"]["Grade"], r["fields"]["Division"])
        for r in fake.rows["Implements"]
    }
    assert (7, "Grade 8", "Boys") in keys
    assert len(fake.rows["Implements"]) == 2 + len(schema.SAMPLE_IMPLEMENTS) - 1


def test_event_names_match_ignoring_case_and_spaces_but_grade_and_division_exactly():
    fake = full_fake()
    fake.rows["Events"] = [
        {"id": i, "fields": {"Name": n, "Kind": "Time"}}
        for i, n in enumerate(["discus ", " 100M", "SHOT PUT"], 1)
    ]
    fake.rows["Implements"] = [
        {"id": 1, "fields": {"Event": 1, "Grade": "Grade 8", "Division": "Girls", "Weight": "x"}},
        {"id": 2, "fields": {"Event": 3, "Grade": "GRADE 9", "Division": "BOYS ", "Weight": "y"}},
    ]
    assert run(fake, "--seed-implements") == 0
    assert len(fake.rows["Events"]) == 3  # no events added
    new = {
        (r["fields"]["Event"], r["fields"]["Grade"], r["fields"]["Division"])
        for r in fake.rows["Implements"][2:]
    }
    assert len(new) == len(schema.SAMPLE_IMPLEMENTS) - 1
    assert (1, "Grade 8", "Girls") not in new  # already there
    assert (3, "Grade 9", "Boys") in new  # "GRADE 9" and "BOYS " do not count as present


# Demo data.


def test_demo_data_is_obviously_fake_and_valid():
    names = {n for n, _, _ in schema.DEMO_ATHLETES}
    assert all(n.startswith("Athlete ") for n in names)
    assert all(m[0].startswith("Demo ") for m in schema.DEMO_MEETS)
    events = {n for n, *_ in schema.EXAMPLE_EVENTS}
    meets = {m[0] for m in schema.DEMO_MEETS}
    for athlete, event, meet, mark, _ in schema.DEMO_RESULTS:
        assert athlete in names and event in events and meet in meets
        assert marks.parse_mark(mark).ok
    for _, iso, _ in schema.DEMO_MEETS:
        assert dates.parse_iso(iso)
    # A personal best to show, and a throwing event with an implement.
    keys = [(a, e) for a, e, *_ in schema.DEMO_RESULTS]
    assert any(keys.count(k) > 1 for k in keys)
    implemented = {
        e
        for e, g, d, _ in schema.SAMPLE_IMPLEMENTS
        for _, ag, ad in schema.DEMO_ATHLETES
        if (g, d) == (ag, ad)
    }
    assert any(e in implemented for _, e, *_ in schema.DEMO_RESULTS)


def test_demo_data_on_a_fresh_document():
    fake = FakeGrist()
    assert run(fake, "--demo-data") == 0
    assert [a["fields"] for a in fake.rows["Athletes"]] == [
        {"Name": n, "Grade": g, "Division": d} for n, g, d in schema.DEMO_ATHLETES
    ]
    assert [m["fields"]["Name"] for m in fake.rows["Meets"]] == [m[0] for m in schema.DEMO_MEETS]
    by = {t: {r["id"]: r["fields"] for r in fake.rows[t]} for t in ("Athletes", "Events", "Meets")}
    results = [
        (
            by["Athletes"][r["fields"]["Athlete"]]["Name"],
            by["Events"][r["fields"]["Event"]]["Name"],
            by["Meets"][r["fields"]["Meet"]]["Name"],
            r["fields"]["Mark"],
        )
        for r in fake.rows["Results"]
    ]
    assert results == [(a, e, m, k) for a, e, m, k, _ in schema.DEMO_RESULTS]
    for r in fake.rows["Results"]:  # Grade, Division and the rest are left to Grist
        assert set(r["fields"]) == {"Athlete", "Event", "Meet", "Mark", "Place"}
    assert len(fake.rows["Implements"]) == len(schema.SAMPLE_IMPLEMENTS)
    assert set(event_names(fake)) == set(implement_event_names()) | {
        r[1] for r in schema.DEMO_RESULTS
    }


def test_demo_data_reuses_existing_events_whatever_their_case_and_spacing():
    fake = full_fake()
    for r in fake.rows["Events"]:
        r["fields"]["Name"] = f" {r['fields']['Name'].upper()} "
    assert run(fake, "--demo-data") == 0
    assert len(fake.rows["Events"]) == len(schema.EXAMPLE_EVENTS)
    assert len(fake.rows["Results"]) == len(schema.DEMO_RESULTS)


@pytest.mark.parametrize("table", ["Athletes", "Meets", "Results"])
def test_demo_data_is_refused_when_a_table_has_rows(capsys, table):
    fake = full_fake()
    del fake.tables["Meets"]["Location"]  # even with other parts missing, nothing is written
    fake.rows[table] = [{"id": 1, "fields": {}}]
    assert run(fake, "--demo-data") == 1
    err = capsys.readouterr().err
    assert table in err and "Nothing was changed" in err
    assert fake.writes == []


def test_demo_data_twice_is_refused_the_second_time():
    fake = FakeGrist()
    assert run(fake, "--demo-data") == 0
    fake.writes.clear()
    assert run(fake, "--demo-data") == 1
    assert run(fake) == 0  # without the flag it is a plain no-op
    assert fake.writes == []


def test_demo_data_is_checked_again_when_applied():
    fake = full_fake()
    # Reads of Results: the plan-time check, then the apply-time check.
    before_nth_get(
        fake,
        "/tables/Results/records",
        2,
        lambda: fake.rows.update(Results=[{"id": 1, "fields": {}}]),
    )
    assert run(fake, "--demo-data") == 1
    assert [w for w in fake.writes if "Implements" not in w[1]] == []  # only the earlier seed


# Grist's default table.


def test_create_removes_the_default_table_and_a_rerun_is_a_noop():
    fake = FakeGrist()
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    assert "Table1" not in fake.tables and removals(fake) == [["RemoveTable", "Table1"]]
    assert set(fake.tables) == {t.id for t in schema.TABLES} | {schema.SUMMARY.id}
    fake.writes.clear()
    assert run(fake) == 0
    assert fake.writes == []


REASONS = {
    "rows": "it has rows",
    "tables": "the document has other tables (Notes)",
    "columns": "it has columns other than A, B, C (Name)",
}


@pytest.mark.parametrize(
    ("new_tables", "new_rows", "reason"),
    [
        ({"Table1": DEFAULTS}, {"Table1": [{"id": 1}]}, REASONS["rows"]),
        ({"Table1": DEFAULTS, "Notes": {}}, {}, REASONS["tables"]),
        ({"Table1": {**DEFAULTS, "Name": {"type": "Text"}}}, {}, REASONS["columns"]),
    ],
)
def test_default_table_is_kept_with_a_warning_when_a_condition_fails(
    capsys, new_tables, new_rows, reason
):
    fake = FakeGrist(new_doc_tables=new_tables)
    fake.new_doc_rows = new_rows
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    assert "Table1" in fake.tables and removals(fake) == [] and "Athletes" in fake.tables
    assert capsys.readouterr().out.count(f"Table1 not removed: {reason}") == 1


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda f: f.rows.update(Table1=[{"id": 1}]), REASONS["rows"]),
        (lambda f: f.tables.update(Notes={}), REASONS["tables"]),
        (lambda f: f.tables["Table1"].update(Name={"type": "Text"}), REASONS["columns"]),
    ],
)
def test_default_table_is_fully_rechecked_right_before_removal(capsys, change, reason):
    fake = FakeGrist()
    # Reads of the tables: the first check, then the one between it and the removal.
    before_nth_get(fake, "/NEWDOC/tables", 2, lambda: change(fake))
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    assert "Table1" in fake.tables and removals(fake) == []
    assert f"Table1 not removed: {reason}" in capsys.readouterr().out
