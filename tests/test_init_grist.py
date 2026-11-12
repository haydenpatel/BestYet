import io
import json
import socket
import ssl
import sys

import httpx
import pytest
from fake_grist import BASE, DOC, KEY, FakeGrist

from bestyet import dates, marks, schema
from bestyet.config import Config
from bestyet.grist import GristClient
from bestyet.init_grist import build_plan, column_payload, main

ENV = {"GRIST_URL": BASE, "GRIST_DOC_ID": DOC, "GRIST_API_KEY": KEY}


def plain(table):
    return [c for c in table.columns if c.formula is None]


def full_tables():
    """A fully initialised document: contract tables, summary table, names shown."""
    tables = {
        t.id: {
            c.id: {"type": c.type, **({"visibleCol": 1} if c.visible_col else {})}
            for c in t.columns
        }
        for t in schema.TABLES
    }
    tables[schema.SUMMARY.id] = {"count": {"type": "Int"}, "Best": {"type": "Numeric"}}
    tables[schema.SUMMARY.id].update(
        {g: {"type": "Ref:X", "visibleCol": 1} for g in schema.SUMMARY.group_by[:2]}
    )
    return tables


def removals(fake):
    return [a for a in fake.actions if a[0] == "RemoveTable"]


def full_fake(tables=None):
    fake = FakeGrist(tables if tables is not None else full_tables())
    if "Events" in fake.tables:  # a fully initialised document has its example Events
        fake.rows["Events"] = [
            {"id": i, "fields": {"Name": n, "Kind": k}}
            for i, (n, k) in enumerate(schema.EXAMPLE_EVENTS, 1)
        ]
    fake.summary_tables.add(schema.SUMMARY.id)
    fake.views[1] = schema.SUMMARY.page
    return fake


ENV_NO_DOC = {k: v for k, v in ENV.items() if k != "GRIST_DOC_ID"}
CREATE = ("--create", "--workspace", "Work")


def run(fake, *argv, env=ENV, yes=True):
    """Run the CLI. Adds --yes (the prompt has its own tests) unless yes=False."""
    if yes:
        argv += ("--yes",)
    return main(list(argv), env, fake.transport)


class Stdin(io.StringIO):
    def __init__(self, text="", tty=True):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


def spy_first_write(fake, capsys):
    """Capture what was printed before the first non-GET request reached the fake."""
    seen = []
    orig = fake.handle

    def handle(request):
        if request.method != "GET" and not seen:
            seen.append(capsys.readouterr().out)
        return orig(request)

    fake.transport = httpx.MockTransport(handle)
    return seen


def test_fresh_doc_gets_the_whole_contract(capsys):
    fake = FakeGrist()
    assert run(fake) == 0
    assert set(fake.tables) == {t.id for t in schema.TABLES} | {schema.SUMMARY.id}
    for t in schema.TABLES:
        assert set(fake.tables[t.id]) == {c.id for c in t.columns}
    assert fake.tables["Results"]["Athlete"]["type"] == "Ref:Athletes"
    assert fake.tables["Meets"]["Date"] == {"type": "Date"}
    out = capsys.readouterr().out
    assert "demo data" not in out
    assert "seed rows" not in out


def event_names(fake):
    return [r["fields"]["Name"] for r in fake.rows.get("Events", [])]


def test_fresh_doc_seeds_events_only_by_default():
    fake = FakeGrist()
    assert run(fake) == 0
    assert [(r["fields"]["Name"], r["fields"]["Kind"]) for r in fake.rows["Events"]] == list(
        schema.EXAMPLE_EVENTS
    )
    assert "Implements" not in fake.rows


def test_seed_implements_resolves_event_ids():
    fake = FakeGrist()
    assert run(fake, "--seed-implements") == 0
    ids = {r["fields"]["Name"]: r["id"] for r in fake.rows["Events"]}
    got = [
        (r["fields"]["Event"], r["fields"]["Grade"], r["fields"]["Weight"])
        for r in fake.rows["Implements"]
    ]
    assert got == [(ids[e], g, w) for e, g, w in schema.EXAMPLE_IMPLEMENTS]


def test_seeding_twice_changes_nothing(capsys):
    fake = FakeGrist()
    assert run(fake, "--seed-implements") == 0
    before = json.dumps(fake.rows)
    fake.writes.clear()
    capsys.readouterr()
    assert run(fake, "--seed-implements") == 0
    assert fake.writes == [] and json.dumps(fake.rows) == before
    assert "Nothing to do" in capsys.readouterr().out


def test_existing_rows_are_kept_and_not_duplicated(capsys):
    fake = full_fake()
    fake.rows["Events"] = [
        {"id": 7, "fields": {"Name": "Discus", "Kind": "Time"}},  # differs: left alone
        {"id": 9, "fields": {"Name": "My Event", "Kind": "Time"}},
    ]
    fake.rows["Implements"] = [
        {"id": 1, "fields": {"Event": 7, "Grade": "Grade 8", "Weight": "999g"}},
        {"id": 2, "fields": {"Event": 9, "Grade": "Grade 9", "Weight": "1kg"}},
    ]
    assert run(fake, "--seed-implements") == 0
    names = event_names(fake)
    assert names.count("Discus") == 1 and "My Event" in names
    assert len(names) == len(schema.EXAMPLE_EVENTS) + 1
    assert fake.rows["Events"][0]["fields"]["Kind"] == "Time"
    imps = [
        (r["fields"]["Event"], r["fields"]["Grade"], r["fields"]["Weight"])
        for r in fake.rows["Implements"]
    ]
    assert imps[:2] == [(7, "Grade 8", "999g"), (9, "Grade 9", "1kg")]
    assert (7, "Grade 8", "500g") not in imps and (7, "Grade 9", "500g") in imps
    assert len(imps) == 2 + len(schema.EXAMPLE_IMPLEMENTS) - 1


def test_case_and_space_variants_are_not_re_added():
    fake = full_fake()
    variants = ["discus ", " 100M", "SHOT PUT"]
    fake.rows["Events"] = [
        {"id": i, "fields": {"Name": n, "Kind": "Time"}} for i, n in enumerate(variants, 1)
    ]
    fake.rows["Implements"] = [
        {"id": 1, "fields": {"Event": 1, "Grade": "grade 8 ", "Weight": "x"}},
        {"id": 2, "fields": {"Event": 3, "Grade": "GRADE 9", "Weight": "y"}},
    ]
    before = json.dumps(fake.rows)
    assert run(fake, "--seed-implements") == 0
    added = [r["fields"]["Name"] for r in fake.rows["Events"][3:]]
    assert len(added) == len(schema.EXAMPLE_EVENTS) - 3
    assert not {"Discus", "100m", "Shot Put"} & set(added)
    ids = {r["fields"]["Name"]: r["id"] for r in fake.rows["Events"]}
    new = [(r["fields"]["Event"], r["fields"]["Grade"]) for r in fake.rows["Implements"][2:]]
    assert new == [(1, "Grade 9"), (3, "Grade 8")]  # Discus 9 and Shot Put 8 only
    assert json.loads(before)["Events"] == fake.rows["Events"][:3]
    assert ids["discus "] == 1


def test_conflicts_get_no_seed_steps(capsys):
    tables = full_tables()
    tables["events"] = tables.pop("Events")  # case-only table conflict
    fake = FakeGrist(tables)
    assert run(fake, "--seed-implements") == 1
    assert "example Events" not in capsys.readouterr().out
    assert fake.writes == []


def test_missing_events_table_with_seed_implements_plans_both_in_order():
    fake = FakeGrist()
    assert run(fake, "--seed-implements") == 0  # tables, then Events rows, then Implements
    assert len(fake.rows["Events"]) == len(schema.EXAMPLE_EVENTS)
    assert len(fake.rows["Implements"]) == len(schema.EXAMPLE_IMPLEMENTS)


def test_implements_not_seeded_without_the_flag():
    fake = full_fake()
    assert run(fake) == 0
    assert fake.writes == []


def test_seed_implements_on_complete_events_adds_only_implements():
    fake = full_fake()
    fake.rows["Events"] = [
        {"id": i, "fields": {"Name": n}} for i, (n, _) in enumerate(schema.EXAMPLE_EVENTS, 1)
    ]
    assert run(fake, "--seed-implements") == 0
    assert fake.writes == [("POST", "/api/docs/DOC_ID_HERE/tables/Implements/records")]


def test_dry_run_lists_seed_steps_and_writes_nothing(capsys):
    fake = full_fake()
    fake.rows["Events"] = []
    assert run(fake, "--dry-run", "--seed-implements") == 0
    out = capsys.readouterr().out
    assert "Events (missing ones only)" in out and "Implements (missing ones only)" in out
    assert fake.writes == []


def test_seed_prompt_declined_writes_nothing(monkeypatch):
    monkeypatch.setattr(sys, "stdin", Stdin("n\n"))
    fake = full_fake()
    assert run(fake, "--seed-implements", yes=False) == 0
    assert fake.writes == []


def test_seed_implements_not_allowed_with_check(capsys):
    with pytest.raises(SystemExit):
        main(["--check", "--seed-implements"], ENV, FakeGrist().transport)
    assert "--seed-implements" in capsys.readouterr().err


def test_formula_and_trigger_columns_carry_their_settings():
    fake = FakeGrist()
    run(fake)
    results = fake.tables["Results"]
    for name in ("Value", "Date", "IsPB"):
        col = schema.table("Results").columns[
            [c.id for c in schema.table("Results").columns].index(name)
        ]
        assert results[name]["isFormula"] is True and results[name]["formula"] == col.formula
    grade, implement = results["Grade"], results["Implement"]
    assert grade["isFormula"] is False and grade["formula"] == schema.GRADE_FORMULA
    assert implement["isFormula"] is False and implement["formula"] == schema.IMPLEMENT_FORMULA
    # Triggers fire on new records or a dependency change, and name the column by its ref.
    assert grade["recalcWhen"] == implement["recalcWhen"] == 0
    assert grade["recalcDeps"] == ["L", fake.refs[("Results", "Athlete")]]
    assert implement["recalcDeps"] == ["L", fake.refs[("Results", "Event")]]


def test_summary_table_is_created_through_a_page_and_has_best():
    fake = FakeGrist()
    run(fake)
    assert fake.summary_tables == {schema.SUMMARY.id}
    assert list(fake.views.values()) == ["Personal bests"]
    [create] = [a for a in fake.actions if a[0] == "CreateViewSection"]
    group = [fake.refs[("Results", g)] for g in schema.SUMMARY.group_by]
    assert create[1] == fake.refs[("Results", "")] and create[2] == 0 and create[4] == group
    best = fake.tables[schema.SUMMARY.id]["Best"]
    assert best["isFormula"] is True and best["formula"] == schema.BEST_FORMULA


def test_ref_columns_show_names():
    fake = FakeGrist()
    run(fake)
    for table, col, target in [
        ("Results", "Athlete", "Athletes"),
        ("Results", "Event", "Events"),
        ("Results", "Meet", "Meets"),
        ("Implements", "Event", "Events"),
    ]:
        assert fake.tables[table][col]["visibleCol"] == fake.refs[(target, "Name")]
    athlete = fake.refs[("Results", "Athlete")]
    assert fake.display[("Results", athlete)] == "$Athlete.Name"  # shows names, not just visibleCol
    summary = fake.tables[schema.SUMMARY.id]
    assert summary["Athlete"]["visibleCol"] == fake.refs[("Athletes", "Name")]
    assert summary["Event"]["visibleCol"] == fake.refs[("Events", "Name")]


def test_existing_visible_col_and_page_are_never_changed():
    tables = full_tables()
    tables["Results"]["Athlete"]["visibleCol"] = 77  # the owner's choice
    fake = full_fake(tables)
    fake.views[1] = schema.SUMMARY.page
    assert run(fake) == 0
    assert fake.writes == []
    assert fake.tables["Results"]["Athlete"]["visibleCol"] == 77


def test_visible_col_set_only_where_unset():
    tables = full_tables()
    tables["Results"]["Athlete"]["visibleCol"] = 77
    del tables["Results"]["Event"]["visibleCol"]
    fake = full_fake(tables)
    assert run(fake) == 0
    assert fake.tables["Results"]["Athlete"]["visibleCol"] == 77
    assert fake.tables["Results"]["Event"]["visibleCol"] == fake.refs[("Events", "Name")]


def test_missing_page_is_created_for_an_existing_summary_table():
    fake = full_fake()
    fake.views.clear()
    assert run(fake) == 0
    assert list(fake.views.values()) == ["Personal bests"]
    assert len([a for a in fake.actions if a[0] == "CreateViewSection"]) == 1


def test_page_without_summary_table_is_a_conflict_and_changes_nothing(capsys):
    tables = full_tables()
    del tables[schema.SUMMARY.id]
    fake = FakeGrist(tables)
    fake.views[1] = schema.SUMMARY.page
    assert run(fake) == 1
    assert fake.writes == []
    assert "exists but summary table" in capsys.readouterr().out


def test_plain_table_with_the_summary_name_is_a_conflict(capsys):
    fake = full_fake()
    fake.summary_tables.clear()
    assert run(fake) == 1
    assert fake.writes == []


def test_choice_columns_carry_choices():
    fake = FakeGrist()
    run(fake)
    kind = fake.tables["Events"]["Kind"]
    assert json.loads(kind["widgetOptions"]) == {"choices": ["Time", "Distance"]}
    grade = json.loads(fake.tables["Athletes"]["Grade"]["widgetOptions"])
    assert grade["choices"] == list(schema.DEFAULT_GRADE_CHOICES)


def test_second_run_is_a_noop(capsys):
    fake = FakeGrist()
    run(fake)
    fake.writes.clear()
    capsys.readouterr()
    assert run(fake) == 0
    assert fake.writes == []
    assert "Nothing to do" in capsys.readouterr().out


def test_partial_doc_gets_only_missing_parts(capsys):
    tables = full_tables()
    del tables["Meets"]
    del tables["Results"]["Mark"]
    tables["Athletes"]["Name"] = {"type": "Numeric"}  # wrong type: noted, not changed
    fake = full_fake(tables)
    assert run(fake) == 0
    assert set(fake.tables["Meets"]) == {"Name", "Date", "Location"}
    assert fake.tables["Results"]["Mark"] == {"type": "Text"}
    assert fake.tables["Athletes"]["Name"] == {"type": "Numeric"}
    posts = [w for w in fake.writes if w[0] == "POST"]
    assert len(posts) == 2
    assert "Left as is" in capsys.readouterr().out


def test_unrelated_tables_refused_then_allowed(capsys):
    fake = FakeGrist({"Notes": {"Text": {"type": "Text"}}})
    fake.tables["Notes_summary_X"] = {}
    fake.summary_tables.add("Notes_summary_X")
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert "Notes" in err and "Notes_summary_X" not in err
    assert fake.writes == []
    assert run(fake, "--allow-existing-tables") == 0
    assert "Athletes" in fake.tables
    assert fake.tables["Notes"] == {"Text": {"type": "Text"}}


def test_summary_table_alone_is_not_unrelated():
    fake = full_fake()
    assert run(fake) == 0
    assert fake.writes == []


def test_dry_run_changes_nothing(capsys):
    fake = FakeGrist()
    assert run(fake, "--dry-run", yes=False) == 0
    assert fake.writes == []
    out = capsys.readouterr().out
    assert "Create Athletes, Events" in out and "--- Plan ---" in out


def test_create_in_workspace_by_id(capsys):
    fake = FakeGrist()
    assert run(fake, "--create", "--workspace", "10", env=ENV_NO_DOC) == 0
    assert fake.created_docs == [(10, "BestYet")]
    assert "NEWDOC" in capsys.readouterr().out


def test_create_workspace_by_name_and_custom_name():
    fake = FakeGrist()
    assert run(fake, "--create", "--workspace", "Work", "--name", "My Doc") == 0
    assert fake.created_docs == [(10, "My Doc")]


def test_create_dry_run_creates_nothing(capsys):
    fake = FakeGrist()
    assert run(fake, "--create", "--workspace", "Work", "--dry-run", yes=False) == 0
    assert fake.created_docs == [] and fake.writes == []
    assert "Create 'BestYet' in workspace" in capsys.readouterr().out


def test_workspace_ambiguous_and_org_narrowing(capsys):
    orgs = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    spaces = {1: [{"id": 10, "name": "Work"}], 2: [{"id": 20, "name": "Work"}]}
    fake = FakeGrist(orgs=orgs, workspaces=spaces)
    assert run(fake, "--create", "--workspace", "Work") == 1
    assert "ambiguous" in capsys.readouterr().err
    assert fake.created_docs == []
    assert run(fake, "--create", "--workspace", "Work", "--org", "B") == 0
    assert fake.created_docs == [(20, "BestYet")]


def test_workspace_missing_and_org_missing(capsys):
    fake = FakeGrist()
    assert run(fake, "--create", "--workspace", "Nope") == 1
    assert "not found" in capsys.readouterr().err
    assert run(fake, "--create", "--workspace", "Work", "--org", "Nope") == 1
    assert "Organisation" in capsys.readouterr().err


def test_usage_errors_exit_nonzero(capsys):
    fake = FakeGrist()
    with pytest.raises(SystemExit) as e:
        run(fake, "--create")
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        run(fake, "--workspace", "Work")


def test_config_and_grist_failures_exit_nonzero(capsys):
    fake = FakeGrist()
    assert run(fake, env={"GRIST_URL": BASE}) == 1
    assert "GRIST_API_KEY is missing" in capsys.readouterr().err
    fake.fail_on = "/tables"
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert err.strip().startswith("error: Grist reported a server error (HTTP 500: boom)")
    assert err.count("\n") == 1


def test_failure_midway_is_one_line_and_hides_key(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST /api/docs/DOC_ID_HERE/tables"
    assert run(fake) == 1
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_unexpected_error_is_one_line(capsys):
    class Boom:
        def handle_request(self, request):
            raise RuntimeError(f"secret {KEY}")

    assert main([], ENV, Boom()) == 1
    err = capsys.readouterr().err
    assert KEY not in err and err.count("\n") == 1


def test_key_never_in_output(capsys):
    fake = FakeGrist({"Notes": {}})
    run(fake)
    run(fake, "--allow-existing-tables", "--dry-run", yes=False)
    run(fake, "--allow-existing-tables")
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err


def test_script_has_no_environment_marker():
    import bestyet.init_grist as mod

    with open(mod.__file__) as f:
        assert "TEST" not in f.read()


def test_plan_creates_plain_columns_first_then_formulas():
    steps = build_plan({}).steps
    results = next(s for s in steps if getattr(s, "table", None) and s.table.id == "Results")
    assert {c.id for c in results.columns} == {"Athlete", "Event", "Meet", "Mark", "Place"}
    later = next(
        s for s in steps if getattr(s, "table", None) == "Results" and hasattr(s, "columns")
    )
    assert [c.id for c in later.columns] == ["Grade", "Implement", "Value", "Date", "IsPB"]
    assert steps.index(results) < steps.index(later)
    assert column_payload(schema.table("Meets").columns[1]) == {
        "id": "Date",
        "fields": {"type": "Date"},
    }


def test_client_add_methods_hit_expected_endpoints():
    fake = FakeGrist({"T": {}})
    with GristClient(BASE, DOC, Config.from_env(ENV).grist_api_key, transport=fake.transport) as c:
        c.add_tables([{"id": "U", "columns": [{"id": "A", "fields": {"type": "Text"}}]}])
        c.add_columns("T", [{"id": "B", "fields": {"type": "Int"}}])
    assert fake.tables["U"] == {"A": {"type": "Text"}}
    assert fake.tables["T"] == {"B": {"type": "Int"}}


def test_case_only_column_conflict_warns_and_fails(capsys):
    tables = full_tables()
    tables["Athletes"]["name"] = tables["Athletes"].pop("Name")
    fake = full_fake(tables)
    assert run(fake) == 1
    captured = capsys.readouterr()
    assert "'name'" in captured.out and "'Name'" in captured.out
    assert "Fix them in Grist" in captured.err
    assert fake.writes == []
    assert set(fake.tables["Athletes"]) == {"name", "Grade"}


def test_case_only_table_conflict_warns_and_fails(capsys):
    tables = full_tables()
    tables["meets"] = tables.pop("Meets")
    fake = full_fake(tables)
    assert run(fake) == 1
    assert "'meets'" in capsys.readouterr().out
    assert fake.writes == [] and "Meets" not in fake.tables


def test_renamed_ids_stop_with_error(capsys):
    from bestyet.init_grist import AddColumns, CreateTable

    fake = FakeGrist({"Athletes": {"Name": {"type": "Text"}}})
    client = GristClient(BASE, DOC, Config.from_env(ENV).grist_api_key, transport=fake.transport)
    col = schema.table("Athletes").columns[0]
    with pytest.raises(Exception, match="Name2"):
        AddColumns("Athletes", (col,)).apply(client)
    with pytest.raises(Exception, match="Athletes2"):
        CreateTable(schema.table("Athletes"), (col,)).apply(client)


def test_failure_after_create_repeats_doc_id(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST /api/docs/NEWDOC/tables"
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    err = capsys.readouterr().err
    assert "GRIST_DOC_ID=NEWDOC" in err and "without --create" in err
    assert "--- What was done ---" in err and "Created document" in err
    assert "--- What did not complete ---" in err and KEY not in err


def test_failure_without_create_has_no_doc_hint(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST"
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert "without --create" not in err and "GRIST_DOC_ID=" not in err
    assert "Run bestyet-init again" in err and "--- What did not complete ---" in err


def test_unrelated_tables_columns_not_read():
    fake = FakeGrist({"Notes": {}})
    fake.fail_on = "tables/Notes/columns"
    assert run(fake, "--allow-existing-tables") == 0


# Grist's default table, the plan, and the confirmation prompt.


def test_create_then_rerun_is_a_noop(capsys):
    fake = FakeGrist()
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    out = capsys.readouterr().out
    assert "GRIST_DOC_ID=NEWDOC" in out and "Needs attention" not in out
    assert "Table1" not in fake.tables and removals(fake) == [["RemoveTable", "Table1"]]
    assert set(fake.tables) == {t.id for t in schema.TABLES} | {schema.SUMMARY.id}
    fake.writes.clear()
    assert run(fake) == 0
    assert fake.writes == []
    assert "Nothing to do" in capsys.readouterr().out


DEFAULTS = {c: {"type": "Any"} for c in "ABC"}


@pytest.mark.parametrize(
    ("new_tables", "new_rows", "reason"),
    [
        ({"Table1": DEFAULTS}, {"Table1": [{"id": 1}]}, "it has rows"),
        (
            {"Table1": DEFAULTS, "Notes": {}},
            {},
            "the document has other tables (Notes)",
        ),
        (
            {"Table1": {**DEFAULTS, "Name": {"type": "Text"}}},
            {},
            "it has columns other than A, B, C (Name)",
        ),
    ],
)
def test_default_table_kept_with_warning_when_a_condition_fails(
    capsys, new_tables, new_rows, reason
):
    fake = FakeGrist(new_doc_tables=new_tables)
    fake.new_doc_rows = new_rows
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    out = capsys.readouterr().out
    assert "Table1" in fake.tables and removals(fake) == []
    assert "Athletes" in fake.tables
    warning = f"Table1 not removed: {reason}"
    assert out.count(warning) == 1  # in the closing summary
    line = next(line for line in out.splitlines() if warning in line)
    assert "Needs attention" in out and "--allow-existing-tables" in line
    assert "remove Table1 in Grist" in line


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda f: f.rows.update(Table1=[{"id": 1}]), "it has rows"),
        (lambda f: f.tables.update(Notes={}), "the document has other tables (Notes)"),
        (
            lambda f: f.tables["Table1"].update(Name={"type": "Text"}),
            "it has columns other than A, B, C (Name)",
        ),
    ],
)
def test_default_table_fully_rechecked_right_before_removal(capsys, change, reason):
    fake = FakeGrist()
    orig = fake.handle
    calls = []

    def handle(request):
        if request.method == "GET" and request.url.path.endswith("/NEWDOC/tables"):
            calls.append(1)
            if len(calls) == 2:  # between the first check and the removal
                change(fake)
        return orig(request)

    fake.transport = httpx.MockTransport(handle)
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    assert "Table1" in fake.tables and removals(fake) == []
    assert f"Table1 not removed: {reason}" in capsys.readouterr().out


def test_remove_table_failure_reports_id_and_hint(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST /api/docs/NEWDOC/apply"
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    err = capsys.readouterr().err
    assert "NEWDOC" in err and "GRIST_DOC_ID=NEWDOC" in err
    assert "--allow-existing-tables" in err and "--- Error ---" in err
    assert "Table1" in fake.tables


def test_next_step_depends_on_the_allow_flag(capsys):
    fake = FakeGrist({"Table1": DEFAULTS})
    assert run(fake, "--allow-existing-tables") == 0
    out = capsys.readouterr().out
    summary = next(line for line in out.splitlines() if "To fix:" in line)
    assert "To fix: remove Table1 in Grist" in summary
    assert "--allow-existing-tables" not in summary
    fake = FakeGrist(new_doc_tables={"Table1": DEFAULTS, "Notes": {}})
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    assert "--allow-existing-tables on re-runs" in capsys.readouterr().out


def test_interrupt_at_the_prompt(monkeypatch, capsys):
    class Interrupting(Stdin):
        def readline(self, *a):
            raise KeyboardInterrupt

    monkeypatch.setattr(sys, "stdin", Interrupting())
    fake = FakeGrist()
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 130
    captured = capsys.readouterr()
    assert captured.err == "error: aborted; nothing was changed\n"
    assert fake.created_docs == [] and fake.writes == []


def test_interrupt_during_apply(capsys):
    fake = FakeGrist()
    orig = fake.handle

    def handle(request):
        if request.method == "POST" and request.url.path.endswith("/tables"):
            raise KeyboardInterrupt
        return orig(request)

    fake.transport = httpx.MockTransport(handle)
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 130
    err = capsys.readouterr().err
    assert err.startswith("error: aborted.") and "GRIST_DOC_ID=NEWDOC" in err
    assert err.count("\n") == 1 and "Traceback" not in err

    fake = FakeGrist()
    orig = fake.handle
    fake.transport = httpx.MockTransport(handle)
    assert run(fake) == 130
    assert capsys.readouterr().err == "error: aborted; re-run to add what is still missing\n"


def test_eof_at_the_prompt_ends_the_line(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Stdin(""))
    assert run(FakeGrist(), yes=False) == 0
    assert "[y/N] \nNot applied" in capsys.readouterr().out


def test_dry_run_wins_over_yes(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Stdin("", tty=False))
    fake = FakeGrist()
    assert run(fake, "--yes", "--dry-run", yes=False) == 0
    assert run(fake, *CREATE, "-y", "--dry-run", env=ENV_NO_DOC, yes=False) == 0
    out = capsys.readouterr().out
    assert "--- Plan ---" in out and "Apply these changes" not in out and "done:" not in out
    assert fake.writes == [] and fake.created_docs == []


def test_existing_document_keeps_default_table_and_warns(capsys):
    fake = FakeGrist({"Table1": DEFAULTS})
    assert run(fake, "--allow-existing-tables") == 0
    out = capsys.readouterr().out
    assert "Table1" in fake.tables and removals(fake) == []
    warning = "Table1 not removed: the document was not created by this run"
    assert out.count(warning) == 1 and "Needs attention" in out and "To fix:" in out


def test_refusal_hints_at_default_table_and_create(capsys):
    fake = FakeGrist({"Table1": DEFAULTS})
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert "Table1" in err and "empty Table1" in err and "--create" in err
    assert fake.writes == []
    fake = FakeGrist({"Notes": {}})
    run(fake)
    assert "empty Table1" not in capsys.readouterr().err


def test_plan_is_printed_before_the_first_write(capsys):
    fake = FakeGrist()
    seen = spy_first_write(fake, capsys)
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    before = seen[0]
    assert "--- Plan ---" in before
    assert "Create 'BestYet' in workspace 'Work'" in before
    assert "Table1" not in before  # behind the scenes: not part of the plan
    assert all(t.id in before for t in schema.TABLES)
    assert "Summary" not in before and "Created document" not in before

    capsys.readouterr()
    fake = FakeGrist()
    seen = spy_first_write(fake, capsys)
    assert run(fake) == 0
    assert "--- Plan ---" in seen[0] and "Create Athletes" in seen[0] and "Summary" not in seen[0]
    assert "Done. Added tables Athletes" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("answer", "applied"),
    [
        ("y\n", True),
        ("YES\n", True),
        ("n\n", False),
        ("\n", False),
        ("maybe\n", False),
        ("", False),
    ],
)
def test_prompt_answers(monkeypatch, capsys, answer, applied):
    monkeypatch.setattr(sys, "stdin", Stdin(answer))
    fake = FakeGrist()
    assert run(fake, yes=False) == 0
    assert "Apply these changes? [y/N]" in capsys.readouterr().out
    assert bool(fake.writes) is applied
    assert ("Athletes" in fake.tables) is applied


def test_prompt_for_create_writes_nothing_on_no(monkeypatch):
    monkeypatch.setattr(sys, "stdin", Stdin("n\n"))
    fake = FakeGrist()
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 0
    assert fake.created_docs == [] and fake.writes == []


def test_non_tty_without_yes_refuses(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Stdin("y\n", tty=False))
    fake = FakeGrist()
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 1
    captured = capsys.readouterr()
    assert "pass --yes to apply" in captured.err
    assert captured.err.count("\n") == 1 and "Apply these changes" not in captured.out
    assert "--- Plan ---" in captured.out
    assert fake.created_docs == [] and fake.writes == []


def test_yes_skips_the_prompt(monkeypatch, capsys):
    class NoRead(Stdin):
        def readline(self, *a):
            raise AssertionError("prompted")

    monkeypatch.setattr(sys, "stdin", NoRead())
    fake = FakeGrist()
    assert main(["-y"], ENV, fake.transport) == 0
    assert "Apply these changes" not in capsys.readouterr().out
    assert "Athletes" in fake.tables


def test_dry_run_and_nothing_to_do_never_prompt(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Stdin("", tty=False))
    fake = FakeGrist()
    assert run(fake, "--dry-run", yes=False) == 0
    out = capsys.readouterr().out
    assert "--- Plan ---" in out and "Apply these changes" not in out and fake.writes == []
    assert run(fake, *CREATE, "--dry-run", env=ENV_NO_DOC, yes=False) == 0
    assert "Create 'BestYet'" in capsys.readouterr().out
    assert fake.created_docs == [] and fake.writes == []
    done = full_fake()
    assert run(done, yes=False) == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_failed_page_rename_names_the_stray_page(capsys):
    fake = FakeGrist()
    orig = fake.handle

    def handle(request):
        # let CreateViewSection through, fail the rename that follows it
        if b"_grist_Views" in request.content:
            return httpx.Response(500, json={"error": "boom"})
        return orig(request)

    fake.transport = httpx.MockTransport(handle)
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert "was created but not named 'Personal bests'" in err and "view 1" in err
    assert KEY not in err


def test_failed_trigger_update_names_the_columns(capsys):
    fake = FakeGrist()
    orig = fake.handle

    def handle(request):
        if request.method == "PATCH" and b"recalcDeps" in request.content:
            return httpx.Response(500, json={"error": "boom"})
        return orig(request)

    fake.transport = httpx.MockTransport(handle)
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert "Results.Grade, Results.Implement" in err and "re-run leaves" in err


# --demo-data


def demo_rows(fake):
    """Results as (athlete, event, meet, mark) names, read back through the ids."""
    by = {t: {r["id"]: r["fields"] for r in fake.rows[t]} for t in ("Athletes", "Events", "Meets")}
    return [
        (
            by["Athletes"][r["fields"]["Athlete"]]["Name"],
            by["Events"][r["fields"]["Event"]]["Name"],
            by["Meets"][r["fields"]["Meet"]]["Name"],
            r["fields"]["Mark"],
        )
        for r in fake.rows["Results"]
    ]


def test_demo_data_is_obviously_fake_and_valid():
    names = {n for n, _ in schema.DEMO_ATHLETES}
    assert all(n.startswith("Athlete ") for n in names)
    assert all(m[0].startswith("Demo ") for m in schema.DEMO_MEETS)
    events = {n for n, _ in schema.EXAMPLE_EVENTS}
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
        e for e, g, _ in schema.EXAMPLE_IMPLEMENTS for a, ag in schema.DEMO_ATHLETES if g == ag
    }
    assert any(e in implemented for _, e, *_ in schema.DEMO_RESULTS)


def test_demo_data_on_fresh_doc(capsys):
    fake = FakeGrist()
    assert run(fake, "--demo-data") == 0
    assert [a["fields"] for a in fake.rows["Athletes"]] == [
        {"Name": n, "Grade": g} for n, g in schema.DEMO_ATHLETES
    ]
    assert [m["fields"]["Name"] for m in fake.rows["Meets"]] == [m[0] for m in schema.DEMO_MEETS]
    assert demo_rows(fake) == [(a, e, m, k) for a, e, m, k, _ in schema.DEMO_RESULTS]
    assert len(fake.rows["Implements"]) == len(schema.EXAMPLE_IMPLEMENTS)  # seeded as needed
    assert len(fake.rows["Events"]) == len(schema.EXAMPLE_EVENTS)
    out = capsys.readouterr().out
    assert out.index("Events (missing") < out.index("demo data (3 athletes")


def test_demo_results_leave_computed_columns_to_grist():
    fake = FakeGrist()
    assert run(fake, "--demo-data") == 0
    for r in fake.rows["Results"]:
        assert set(r["fields"]) == {"Athlete", "Event", "Meet", "Mark", "Place"}


def test_demo_data_on_existing_empty_doc_reuses_seeded_events():
    fake = full_fake()
    before = json.dumps(fake.rows["Events"])
    assert run(fake, "--demo-data") == 0
    assert json.dumps(fake.rows["Events"]) == before  # none duplicated
    assert len(fake.rows["Results"]) == len(schema.DEMO_RESULTS)


def test_demo_data_matches_events_ignoring_case_and_spaces():
    fake = full_fake()
    for r in fake.rows["Events"]:
        r["fields"]["Name"] = f" {r['fields']['Name'].upper()} "
    assert run(fake, "--demo-data") == 0
    assert len(fake.rows["Events"]) == len(schema.EXAMPLE_EVENTS)
    assert len(fake.rows["Results"]) == len(schema.DEMO_RESULTS)


@pytest.mark.parametrize("table", ["Athletes", "Meets", "Results"])
def test_demo_data_refuses_when_a_table_has_rows(capsys, table):
    fake = full_fake()
    fake.rows[table] = [{"id": 1, "fields": {}}]
    assert run(fake, "--demo-data") == 1
    err = capsys.readouterr().err
    assert table in err and "Nothing was changed" in err
    assert fake.writes == []


def test_demo_data_refuses_even_when_other_parts_are_missing(capsys):
    fake = full_fake()
    del fake.tables["Meets"]["Location"]
    fake.rows["Athletes"] = [{"id": 1, "fields": {"Name": "Real"}}]
    assert run(fake, "--demo-data") == 1
    assert fake.writes == []


def test_demo_data_twice_refuses_second_time(capsys):
    fake = FakeGrist()
    assert run(fake, "--demo-data") == 0
    fake.writes.clear()
    assert run(fake, "--demo-data") == 1
    assert fake.writes == []
    assert run(fake) == 0  # without the flag it is a plain no-op
    assert fake.writes == []


def test_demo_data_dry_run_and_prompt_write_nothing(monkeypatch, capsys):
    fake = full_fake()
    assert run(fake, "--demo-data", "--dry-run") == 0
    assert "demo data (3 athletes" in capsys.readouterr().out and fake.writes == []
    monkeypatch.setattr(sys, "stdin", Stdin("n\n"))
    assert run(fake, "--demo-data", yes=False) == 0
    assert fake.writes == []


def test_demo_data_rechecked_when_applied(capsys):
    """Rows added between the plan and the apply stop the load."""
    fake = full_fake()
    seen = []
    orig = fake.handle

    def handle(request):
        if request.method == "GET" and request.url.path.endswith("/tables/Results/records"):
            seen.append(1)
            if len(seen) == 2:  # relies on 2 reads: plan-time check, then apply-time check
                fake.rows["Results"] = [{"id": 1, "fields": {}}]
        return orig(request)

    fake.transport = httpx.MockTransport(handle)
    assert run(fake, "--demo-data") == 1
    assert [w for w in fake.writes if "Implements" not in w[1]] == []  # only the earlier seed


def test_demo_data_not_allowed_with_check(capsys):
    with pytest.raises(SystemExit):
        main(["--check", "--demo-data"], ENV, FakeGrist().transport)
    assert "--demo-data" in capsys.readouterr().err


def test_demo_data_gets_no_step_on_conflict(capsys):
    tables = full_tables()
    tables["events"] = tables.pop("Events")
    fake = FakeGrist(tables)
    assert run(fake, "--demo-data") == 1
    assert "demo data" not in capsys.readouterr().out
    assert fake.writes == []


# Error messages and the closing summary.


def failing_transport(handler):
    return httpx.MockTransport(handler)


def reply(status, error="denied"):
    return failing_transport(lambda request: httpx.Response(status, json={"error": error}))


def refuse(cause):
    def handler(request):
        raise httpx.ConnectError("failed", request=request) from cause

    return failing_transport(handler)


@pytest.mark.parametrize(
    ("cause", "text"),
    [
        (socket.gaierror(8, "no such host"), "could not be resolved (DNS)"),
        (ConnectionRefusedError(61, "refused"), "connection was refused"),
        (ssl.SSLCertVerificationError("bad certificate"), "TLS or certificate problem"),
        (OSError("x"), "the connection failed"),
    ],
)
def test_unreachable_names_the_host_and_the_cause(capsys, cause, text):
    userinfo = "bob:" + "hunter2"  # built in parts: a literal user:pass URL trips secret scanners
    env = {**ENV, "GRIST_URL": f"https://{userinfo}@grist.example.test:8484/sub"}
    assert main([], env, refuse(cause)) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: Could not reach Grist at grist.example.test:8484: ")
    assert text in err and "Check GRIST_URL" in err
    for secret in (KEY, "hunter2", "bob", "/sub", "https://"):
        assert secret not in err
    assert err.count("\n") == 1


def test_timeout_message(capsys):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert main(["--check"], ENV, failing_transport(handler)) == 1
    assert "at grist.example.test: the connection timed out" in capsys.readouterr().err


def test_unauthorised_says_to_check_the_key(capsys):
    assert main(["--check"], ENV, reply(401, "Unauthorized")) == 1
    err = capsys.readouterr().err
    assert "rejected the API key (HTTP 401)" in err and "GRIST_API_KEY" in err
    assert KEY not in err and err.count("\n") == 1


def test_forbidden_says_what_to_check(capsys):
    assert main(["--check"], ENV, reply(403)) == 1
    assert "can access the document" in capsys.readouterr().err
    assert main([*CREATE, "--yes"], ENV_NO_DOC, reply(403)) == 1
    assert "can access the workspace" in capsys.readouterr().err


def test_unknown_document(capsys):
    assert main(["--yes"], ENV, reply(404, "not found")) == 1
    err = capsys.readouterr().err
    assert "could not find the document (HTTP 404)" in err and "GRIST_DOC_ID" in err
    assert DOC not in err


def test_unknown_workspace_by_name_and_by_id(capsys):
    fake = FakeGrist()
    assert run(fake, "--create", "--workspace", "Nope", "--dry-run", env=ENV_NO_DOC) == 1
    err = capsys.readouterr().err
    assert "Workspace 'Nope' not found" in err and "--workspace" in err
    assert fake.writes == []
    assert main(["--create", "--workspace", "99", "--yes"], ENV_NO_DOC, reply(404)) == 1
    assert "could not find that workspace (HTTP 404)" in capsys.readouterr().err


def test_not_a_grist_server(capsys):
    transport = failing_transport(lambda request: httpx.Response(200, text="<html>"))
    assert main(["--check"], ENV, transport) == 1
    assert "Check that GRIST_URL is the address of a Grist server" in capsys.readouterr().err


def test_partial_failure_lists_done_and_not_done_for_an_existing_document(capsys):
    fake = full_fake()
    del fake.tables["Meets"]
    fake.fail_on = "POST /api/docs/DOC_ID_HERE/tables/Results/columns"
    del fake.tables["Results"]["Mark"]
    assert run(fake) == 1
    captured = capsys.readouterr()
    err = captured.err
    assert err.lstrip().startswith("--- Error ---\nGrist reported a server error (HTTP 500: boom)")
    assert "The run stopped part-way." in err
    assert "--- What was done ---\n  Tables\n    - Created Meets" in err
    assert (
        "--- What did not complete ---\n  Tables\n    - Add missing columns to Results: Mark (failed; may be partly applied)"
        in err
    )
    assert err.rstrip().endswith("it only adds what is missing.")
    assert "Summary" not in captured.out and KEY not in err + captured.out


def test_partial_failure_with_nothing_done(capsys):
    fake = full_fake()
    del fake.tables["Meets"]
    fake.fail_on = "POST"
    assert run(fake) == 1
    assert "What was done" not in capsys.readouterr().err  # nothing to list


def test_partial_failure_after_create_names_the_document(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST /api/docs/NEWDOC/tables"
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    captured = capsys.readouterr()
    assert "Created the document 'BestYet' (id NEWDOC)." in captured.out  # printed straight away
    assert (
        "--- What was done ---\n  Document\n    - Created document 'BestYet' (id NEWDOC)"
        in captured.err
    )
    assert "Removed Grist's empty default table (Table1)" in captured.err
    assert "without --create, with GRIST_DOC_ID=NEWDOC" in captured.err


def test_create_ends_with_a_short_done_message_and_next_steps(capsys):
    fake = FakeGrist()
    assert run(fake, *CREATE, "--name", "My Doc", env=ENV_NO_DOC) == 0
    captured = capsys.readouterr()
    out = captured.out
    assert captured.err == "" and KEY not in out and "Summary" not in out
    done = out[out.index("Done. Created") :]
    assert (
        "Done. Created 'My Doc' in workspace 'Work' with 5 tables, the 'Personal bests' page"
        in done
    )
    assert "and example data." in done and "Needs attention" not in done
    assert "\n       GRIST_DOC_ID=NEWDOC\n" in done  # on a line of its own
    assert done.index("GRIST_DOC_ID=NEWDOC") < done.index("2. Then run bestyet-init --check")
    assert "saved nothing" in done
    for part in ("Setting up", "Plan", "Next steps"):
        assert f"--- {part} ---" in out
    assert "--- Done ---" not in out


def test_existing_document_done_message_says_what_was_added(capsys):
    fake = full_fake()
    del fake.tables["Meets"]
    assert run(fake) == 0
    out = capsys.readouterr().out
    assert "Done. Added tables Meets." in out
    assert "GRIST_DOC_ID" not in out and "\n  - Run bestyet-init --check" in out


def test_columns_added_to_an_existing_table_are_named(capsys):
    fake = full_fake()
    del fake.tables["Results"]["Mark"]
    assert run(fake) == 0
    assert "Done. Added missing columns in Results." in capsys.readouterr().out


def test_warnings_are_shown_when_the_run_stops_part_way(capsys):
    fake = FakeGrist({"Table1": DEFAULTS})
    fake.fail_on = "POST /api/docs/DOC_ID_HERE/tables"
    assert run(fake, "--allow-existing-tables") == 1
    err = capsys.readouterr().err
    assert "--- Next steps ---" in err and "Table1 not removed" in err and "To fix:" in err


def _fail_page_rename(fake):
    orig = fake.handle

    def handle(request):
        if b"_grist_Views" in request.content:  # the rename after CreateViewSection
            return httpx.Response(500, json={"error": "boom"})
        return orig(request)

    fake.transport = httpx.MockTransport(handle)


def _fail_trigger_update(fake):
    orig = fake.handle

    def handle(request):
        if request.method == "PATCH" and b"recalcDeps" in request.content:
            return httpx.Response(500, json={"error": "boom"})
        return orig(request)

    fake.transport = httpx.MockTransport(handle)


@pytest.mark.parametrize(
    ("break_it", "problem", "failed"),
    [
        (
            _fail_page_rename,
            "was created but not named 'Personal bests'",
            "Create a 'Personal bests' page",
        ),
        (
            _fail_trigger_update,
            "Created without its trigger dependency",
            "Calculated columns",
        ),
    ],
)
def test_create_with_a_failure_a_rerun_cannot_fix_keeps_the_id_and_does_not_say_rerun(
    capsys, break_it, problem, failed
):
    fake = FakeGrist()
    break_it(fake)
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    err = capsys.readouterr().err
    assert problem in err and "(failed; may be partly applied)" in err and failed in err
    assert "id NEWDOC" in err and "GRIST_DOC_ID=NEWDOC" in err
    assert "Fix the problem described under Error in Grist" in err
    assert "To continue, re-run" not in err and "it only adds what is missing" not in err
    assert "Do not run --create again" in err and KEY not in err


def test_failed_step_is_separated_from_the_steps_not_done(capsys):
    fake = full_fake()
    del fake.tables["Meets"]
    del fake.tables["Results"]["Mark"]
    fake.fail_on = "POST /api/docs/DOC_ID_HERE/tables/Results/columns"
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert (
        "--- What did not complete ---\n  Tables\n    - Add missing columns to Results: Mark (failed; may be partly applied)"
        in err
    )
    assert "What was not done" not in err  # it was the last step
    fake = FakeGrist()
    fake.fail_on = "POST /api/docs/DOC_ID_HERE/tables"
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert (
        "--- What did not complete ---\n  Tables\n    - Create Athletes (failed; may be partly applied)\n    - Create Events"
        in err
    )
    assert "(failed; may be partly applied)" in err
    assert "Personal bests\n    - Create a 'Personal bests' page" in err  # not started: unlabelled


def test_plan_is_grouped_in_plain_words(capsys):
    fake = FakeGrist()
    assert run(fake, *CREATE, "--seed-implements", "--demo-data", "--dry-run", env=ENV_NO_DOC) == 0
    out = capsys.readouterr().out
    headings = [
        line.strip()
        for line in out.splitlines()
        if line.startswith("  ") and not line.startswith("    ")
    ]
    assert headings == ["Document", "Tables", "Personal bests", "Example data"]
    assert "Create 'BestYet' in workspace 'Work'" in out
    assert "showing each athlete's best per event" in out
    assert "(3 athletes, 2 meets, 7 results; only if those tables are empty)" in out
    for hidden in ("Calculated", "Display", "Table1", "worked out", "Ref columns"):
        assert hidden not in out  # behind the scenes: done, not announced
    assert "Results_summary" not in out and "columns)" not in out and "(no rows" not in out


def test_workspace_given_by_id_is_shown_by_name_when_known(capsys):
    fake = FakeGrist()
    assert run(fake, "--create", "--workspace", "10", "--dry-run", env=ENV_NO_DOC) == 0
    assert "workspace 'Work'" in capsys.readouterr().out


def test_done_message_hides_internal_ids(capsys):
    assert run(FakeGrist(), *CREATE, env=ENV_NO_DOC) == 0
    out = capsys.readouterr().out
    assert "Results_summary" not in out and "colRef" not in out


# Guided mode.


class TtyOut(io.StringIO):
    def isatty(self):
        return True


class Interrupting(Stdin):
    def readline(self, *args):
        raise KeyboardInterrupt


def terminal(monkeypatch, stdin):
    """Make stdin and stdout look like a terminal."""
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)


def guided(monkeypatch, fake, answers, *argv, env=ENV_NO_DOC):
    """Run the CLI in a terminal with scripted answers (no argv: the guided start)."""
    terminal(monkeypatch, Stdin(answers))
    return main(list(argv), env, fake.transport)


def test_guided_new_document(monkeypatch, capsys):
    fake = FakeGrist()
    answers = "y\nMy Doc\ny\nn\ny\n"  # workspace, name, implements, no demo, apply
    assert guided(monkeypatch, fake, answers) == 0
    out = capsys.readouterr().out
    assert fake.created_docs == [(10, "My Doc")]
    assert len(fake.rows["Implements"]) == len(schema.EXAMPLE_IMPLEMENTS)
    assert "Athletes" not in fake.rows
    assert "Use workspace 'Work'? [Y/n]" in out and "(Org One)" not in out
    assert "Create 'My Doc' in workspace 'Work'" in out and "GRIST_DOC_ID=NEWDOC" in out
    assert KEY not in out


def test_guided_new_document_declined_at_the_confirmation(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "y\nc\n\nn\nn\nn\n") == 0
    out = capsys.readouterr().out
    assert "--- Plan ---" in out and "Not applied" in out
    assert fake.created_docs == [] and fake.writes == []


def test_guided_default_name_and_demo_data(monkeypatch):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "y\n\nn\ny\ny\n") == 0
    assert fake.created_docs == [(10, "BestYet")]
    assert len(fake.rows["Results"]) == len(schema.DEMO_RESULTS)
    assert len(fake.rows["Implements"]) == len(schema.EXAMPLE_IMPLEMENTS)  # demo needs them


def test_guided_existing_document(monkeypatch, capsys):
    fake = full_fake()
    del fake.tables["Meets"]
    assert guided(monkeypatch, fake, "y\nn\nn\ny\n", env=ENV) == 0
    assert "Meets" in fake.tables and fake.created_docs == []
    assert "GRIST_DOC_ID=" not in capsys.readouterr().out


def test_guided_demo_is_not_offered_when_the_tables_have_rows(monkeypatch, capsys):
    fake = full_fake()
    fake.rows["Athletes"] = [{"id": 1, "fields": {"Name": "A"}}]
    assert (
        guided(monkeypatch, fake, "y\nn\n", env=ENV) == 0
    )  # existing, no implements; then nothing to do
    out = capsys.readouterr().out
    assert "Add demo data" not in out and "example implements" in out


def test_guided_bad_numbers_are_asked_again(monkeypatch, capsys):
    fake = FakeGrist()
    fake.workspaces = workspace_with("Alpha")
    assert guided(monkeypatch, fake, "x\ny\n0\n7\nc\nDoc\nn\nmaybe\nn\ny\n") == 0
    out = capsys.readouterr().out
    assert "Please enter a number from 1 to 1, or C." in out
    assert "Please answer y or n." in out
    assert fake.created_docs == [(10, "Doc")]


def test_guided_shows_the_organisation_when_there_are_several(monkeypatch, capsys):
    fake = FakeGrist(
        orgs=[{"id": 1, "name": "Org One"}, {"id": 2, "name": "Org Two"}],
        workspaces={1: [{"id": 10, "name": "Work"}], 2: [{"id": 20, "name": "Home"}]},
    )
    assert guided(monkeypatch, fake, "2\nDoc\nn\nn\ny\n") == 0
    assert "2. Home (Org Two)" in capsys.readouterr().out
    assert fake.created_docs == [(20, "Doc")]


@pytest.mark.parametrize("answers", ["", "y\n", "y\nc\n", "y\nc\nDoc\nn\n"])
def test_guided_end_of_input_stops_cleanly(monkeypatch, capsys, answers):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, answers) == 130
    assert "error: aborted; nothing was changed" in capsys.readouterr().err
    assert fake.created_docs == [] and fake.writes == []


def test_guided_ctrl_c_stops_cleanly(monkeypatch, capsys):
    fake = FakeGrist()
    terminal(monkeypatch, Interrupting("", tty=True))
    assert main([], ENV, fake.transport) == 130
    assert "aborted; nothing was changed" in capsys.readouterr().err
    assert fake.writes == []


def test_guided_with_no_visible_workspace(monkeypatch, capsys):
    fake = FakeGrist(workspaces={1: []})
    assert guided(monkeypatch, fake, "") == 1
    assert "cannot see any workspace" in capsys.readouterr().err


def test_guided_without_doc_id_and_without_documents_creates_a_new_one(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "y\nDoc\nn\nn\ny\n", env=ENV_NO_DOC) == 0
    assert "existing document" not in capsys.readouterr().out  # nothing to choose from
    assert fake.created_docs == [(10, "Doc")]


def test_guided_flags_skip_their_questions(monkeypatch, capsys):
    fake = FakeGrist()
    argv = ("--create", "--name", "Given", "--seed-implements")
    assert guided(monkeypatch, fake, "y\nn\ny\n", *argv) == 0  # workspace, demo data, apply
    out = capsys.readouterr().out
    assert "Use workspace 'Work'?" in out and "Name for the new" not in out
    assert "example implements" not in out and "Add demo data" in out
    assert fake.created_docs == [(10, "Given")]


def test_guided_workspace_flag_skips_only_the_workspace_question(monkeypatch, capsys):
    fake = FakeGrist()
    fake.workspaces = workspace_with("Alpha")
    assert guided(monkeypatch, fake, "c\nDoc\nn\nn\ny\n", "--workspace", "Work") == 0
    out = capsys.readouterr().out
    assert "Which workspace" not in out
    assert "Do you want to use an existing document in workspace 'Work'?" in out
    assert fake.created_docs == [(10, "Doc")]


def test_guided_dry_run_shows_the_plan_and_writes_nothing(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "y\nDoc\nn\nn\n", "--create", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "Apply these changes" not in out
    assert fake.created_docs == [] and fake.writes == []


def test_terminal_without_flags_starts_the_guide(monkeypatch, capsys):
    fake = FakeGrist()
    monkeypatch.setattr(sys, "stdin", Stdin("y\nDoc\nn\nn\ny\n", tty=True))
    monkeypatch.setattr(sys, "stdout", TtyOut())
    assert main([], ENV_NO_DOC, fake.transport) == 0
    assert fake.created_docs == [(10, "Doc")]


@pytest.mark.parametrize("argv", [["--check"], ["--dry-run"], ["--yes"], ["--demo-data"]])
def test_flags_in_a_terminal_never_prompt(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "stdin", Stdin("", tty=True))
    stdout = TtyOut()
    monkeypatch.setattr(sys, "stdout", stdout)
    main(argv, ENV, full_fake().transport)
    assert "BestYet setup" not in stdout.getvalue()


def test_no_terminal_never_prompts(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Stdin("1\n", tty=False))
    fake = full_fake()
    del fake.tables["Meets"]
    assert main([], ENV, fake.transport) == 1
    captured = capsys.readouterr()
    assert "BestYet setup" not in captured.out and "without options" in captured.err
    assert fake.writes == []


def test_missing_values_are_usage_errors_without_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Stdin("y\n", tty=False))
    for argv, message in (
        (["--create"], "--create needs --workspace"),
        (["--workspace", "Work"], "only apply with --create"),
        (["--org", "Org One"], "only apply with --create"),
    ):
        with pytest.raises(SystemExit):
            main(argv, ENV, FakeGrist().transport)
        assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "answers"),
    [
        (["--create"], "y\nDoc\nn\nn\ny\n"),  # asks the workspace, then the rest
        (["--create", "--yes"], "y\nDoc\nn\nn\n"),  # --yes still skips the confirmation
    ],
)
def test_create_without_workspace_asks_for_it_in_a_terminal(monkeypatch, capsys, argv, answers):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, answers, *argv, env=ENV_NO_DOC) == 0
    assert "Use workspace 'Work'?" in capsys.readouterr().out
    assert fake.created_docs == [(10, "Doc")]


def test_fully_specified_options_in_a_terminal_ask_nothing(monkeypatch, capsys):
    fake = FakeGrist()
    terminal(monkeypatch, Stdin("", tty=True))
    assert main([*CREATE, "--yes"], ENV_NO_DOC, fake.transport) == 0
    assert "BestYet setup" not in capsys.readouterr().out and fake.created_docs == [(10, "BestYet")]


def test_a_lone_workspace_is_confirmed_not_assumed(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "n\n") == 0
    out = capsys.readouterr().out
    assert "Use workspace 'Work'? [Y/n]" in out and "no other workspace" in out
    assert "Create one in Grist, then run this again. Nothing was changed." in out
    assert fake.created_docs == [] and fake.writes == []


def test_several_workspaces_are_listed_without_a_yes_no_question(monkeypatch, capsys):
    fake = FakeGrist(workspaces={1: [{"id": 10, "name": "Work"}, {"id": 11, "name": "Home"}]})
    assert guided(monkeypatch, fake, "2\nDoc\nn\nn\ny\n") == 0
    out = capsys.readouterr().out
    assert "2. Home" in out and "Use workspace" not in out
    assert fake.created_docs == [(11, "Doc")]


# Documents already in the workspace, and duplicate names.


def workspace_with(*names):
    docs = [{"id": f"DOC{i}", "name": n} for i, n in enumerate(names)]
    return {1: [{"id": 10, "name": "Work", "docs": docs}]}


def test_guided_lists_the_workspace_documents_and_sets_up_the_chosen_one(monkeypatch, capsys):
    fake = full_fake()
    del fake.tables["Meets"]
    fake.workspaces = workspace_with("Alpha", "Beta")
    assert guided(monkeypatch, fake, "y\n2\nn\nn\ny\n", env=ENV_NO_DOC) == 0  # Beta, no extras
    out = capsys.readouterr().out
    assert "1. Alpha" in out and "2. Beta" in out and "or C to create a new document:" in out
    assert "GRIST_DOC_ID" not in out.split("--- Plan ---")[0].split("2. Beta")[1]  # unset
    assert "Meets" in fake.tables and fake.created_docs == []
    assert any("/api/docs/DOC1/" in path for _, path in fake.writes)  # the chosen document


def test_guided_duplicate_name_a_no_asks_for_another_name(monkeypatch, capsys):
    fake = FakeGrist()
    fake.workspaces = workspace_with("My Doc")
    answers = "y\nc\n  my doc \nn\nOther\nn\nn\ny\n"  # clash, no, another name, ...
    assert guided(monkeypatch, fake, answers, env=ENV_NO_DOC) == 0
    out = capsys.readouterr().out
    assert out.count("A document named 'My Doc' already exists in this workspace.") == 1
    assert "Use it instead? [Y/n]" in out and "DOC0" not in out
    assert fake.created_docs == [(10, "Other")]


def test_guided_duplicate_name_a_yes_sets_up_that_document(monkeypatch, capsys):
    fake = FakeGrist()
    fake.workspaces = workspace_with("My Doc")
    assert guided(monkeypatch, fake, "y\nc\nmy doc\n\nn\nn\ny\n", env=ENV_NO_DOC) == 0
    assert fake.created_docs == []  # never a second document
    assert set(fake.tables) >= {t.id for t in schema.TABLES}
    assert all("/api/docs/DOC0/" in path for _, path in fake.writes)


def test_guided_given_duplicate_name_stops_on_a_no(monkeypatch, capsys):
    fake = FakeGrist()
    fake.workspaces = workspace_with("My Doc")
    argv = ("--create", "--name", "my doc")
    assert guided(monkeypatch, fake, "y\nn\n", *argv) == 0
    assert "Nothing was changed" in capsys.readouterr().out
    assert fake.created_docs == [] and fake.writes == []


def test_same_name_in_another_workspace_is_not_a_clash(monkeypatch, capsys):
    fake = FakeGrist(
        workspaces={
            1: [
                {"id": 10, "name": "Work", "docs": [{"id": "D", "name": "BestYet"}]},
                {"id": 11, "name": "Home", "docs": []},
            ]
        }
    )
    assert run(fake, "--create", "--workspace", "Home", env=ENV_NO_DOC) == 0
    assert fake.created_docs == [(11, "BestYet")]


@pytest.mark.parametrize("name", ["BestYet", "bestyet", "  BESTYET  "])
def test_flag_form_refuses_a_duplicate_with_yes(capsys, name):
    fake = FakeGrist()
    fake.workspaces = workspace_with("BestYet")
    assert run(fake, "--create", "--workspace", "Work", "--name", name, env=ENV_NO_DOC) == 1
    err = capsys.readouterr().err
    assert "A document named 'BestYet' already exists in workspace 'Work'" in err
    assert "--name" in err and "DOC0" not in err
    assert fake.created_docs == [] and fake.writes == []


def test_flag_form_refuses_a_duplicate_without_a_terminal(monkeypatch, capsys):
    fake = FakeGrist()
    fake.workspaces = workspace_with("BestYet")
    monkeypatch.setattr(sys, "stdin", Stdin("y\n", tty=False))
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 1
    assert "already exists" in capsys.readouterr().err and fake.created_docs == []


def test_flag_form_asks_in_a_terminal(monkeypatch, capsys):
    fake = FakeGrist()
    fake.workspaces = workspace_with("BestYet")
    monkeypatch.setattr(sys, "stdin", Stdin("n\n", tty=True))
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 0
    out = capsys.readouterr().out
    assert "Use it instead? [Y/n]" in out and "Nothing was changed" in out
    assert fake.created_docs == [] and fake.writes == []
    monkeypatch.setattr(sys, "stdin", Stdin("y\ny\n", tty=True))  # use it, apply
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 0
    assert fake.created_docs == []  # never a second document
    assert fake.writes and all("/api/docs/DOC0/" in path for _, path in fake.writes)


def test_flag_form_end_of_input_at_the_duplicate_question_creates_nothing(monkeypatch, capsys):
    fake = FakeGrist()
    fake.workspaces = workspace_with("BestYet")
    monkeypatch.setattr(sys, "stdin", Stdin("", tty=True))
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 130
    assert fake.created_docs == [] and fake.writes == []


def test_dry_run_in_a_terminal_shows_the_plan_for_using_the_existing_document(monkeypatch, capsys):
    fake = FakeGrist()
    fake.workspaces = workspace_with("BestYet")
    monkeypatch.setattr(sys, "stdin", Stdin("", tty=True))
    assert run(fake, *CREATE, "--dry-run", env=ENV_NO_DOC, yes=False) == 0
    out = capsys.readouterr().out
    assert "already exists in workspace 'Work'" in out and "asks whether to use it" in out
    assert "Create 'BestYet'" not in out and "--- Plan ---" in out
    assert fake.created_docs == [] and fake.writes == []


def test_dry_run_that_a_real_run_would_refuse_is_refused(capsys):
    fake = FakeGrist()
    fake.workspaces = workspace_with("BestYet")
    assert run(fake, *CREATE, "--dry-run", env=ENV_NO_DOC) == 1  # --yes: no switching unseen
    assert "already exists" in capsys.readouterr().err
    assert fake.created_docs == [] and fake.writes == []


def test_duplicate_is_checked_again_right_before_creating(capsys):
    fake = FakeGrist()
    orig = fake.handle
    listings = []

    def handle(request):
        if request.method == "GET" and request.url.path.endswith("/workspaces"):
            listings.append(1)
            if len(listings) >= 3:  # the third look is the one right before creating
                fake.workspaces = workspace_with("BestYet")
        return orig(request)

    fake.transport = httpx.MockTransport(handle)
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    assert "already exists" in capsys.readouterr().err and fake.created_docs == []


def test_listing_without_docs_falls_back_to_the_workspace_itself(capsys):
    fake = FakeGrist()
    fake.workspaces = {1: [{"id": 10, "name": "Work"}]}  # no "docs" in the listing
    orig = fake.handle

    def handle(request):
        if request.method == "GET" and request.url.path == "/api/workspaces/10":
            return httpx.Response(200, json={"id": 10, "docs": [{"id": "D", "name": "BestYet"}]})
        return orig(request)

    fake.transport = httpx.MockTransport(handle)
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    assert "already exists" in capsys.readouterr().err


def test_existing_document_runs_do_not_look_at_workspaces(capsys):
    fake = full_fake()
    fake.fail_on = "workspaces"
    assert run(fake) == 0


# Other guided-mode and wording fixes.


def test_applying_line_has_no_internal_step_count(capsys):
    assert run(FakeGrist(), *CREATE, env=ENV_NO_DOC) == 0
    out = capsys.readouterr().out
    assert "--- Setting up ---" in out and "Applying" not in out


def test_guided_org_alone_picks_from_that_organisation(monkeypatch, capsys):
    fake = FakeGrist(
        orgs=[{"id": 1, "name": "Org One"}, {"id": 2, "name": "Org Two"}],
        workspaces={1: [{"id": 10, "name": "Work"}], 2: [{"id": 20, "name": "Home"}]},
    )
    assert guided(monkeypatch, fake, "y\nDoc\nn\nn\ny\n", "--org", "Org Two") == 0
    out = capsys.readouterr().out
    assert "Use workspace 'Home'?" in out and "Work" not in out
    assert fake.created_docs == [(20, "Doc")]


def test_guided_org_that_does_not_exist_says_so(monkeypatch, capsys):
    assert guided(monkeypatch, FakeGrist(), "", "--org", "Nope") == 1
    err = capsys.readouterr().err
    assert "Organisation 'Nope' not found" in err and "cannot see any workspace" not in err


def test_guided_existing_document_with_other_tables_asks_up_front(monkeypatch, capsys):
    fake = full_fake({**full_tables(), "Notes": {"A": {"type": "Text"}}})
    del fake.tables["Meets"]
    assert guided(monkeypatch, fake, "y\ny\nn\nn\ny\n", env=ENV) == 0
    out = capsys.readouterr().out
    assert "not part of BestYet (Notes)" in out and out.index("Notes") < out.index(
        "example implements"
    )
    assert "Meets" in fake.tables
    fake = full_fake({**full_tables(), "Notes": {"A": {"type": "Text"}}})
    assert guided(monkeypatch, fake, "y\nn\n", env=ENV) == 0
    assert "Nothing was changed" in capsys.readouterr().out and fake.writes == []


def test_behind_the_scenes_steps_are_left_out_of_the_plan_but_named_when_they_fail(capsys):
    fake = full_fake()
    del fake.tables["Results"]["Value"]  # a calculated column: only fixed up, never announced
    assert run(fake, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Update the existing tables" in out and "Value" not in out
    fake.fail_on = "POST /api/docs/DOC_ID_HERE/tables/Results/columns"
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert "--- What did not complete ---\n  Calculated columns\n    - Results: Value" in err


def test_failed_default_table_removal_is_reported_not_planned(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST /api/docs/NEWDOC/apply"
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    out = capsys.readouterr()
    assert "Table1" not in out.out.split("--- Setting up ---")[0]
    assert "--allow-existing-tables" in out.err


def test_grist_doc_id_is_offered_first_by_name(monkeypatch, capsys):
    fake = full_fake()
    del fake.tables["Meets"]
    assert guided(monkeypatch, fake, "y\nn\nn\ny\n", env=ENV) == 0
    out = capsys.readouterr().out
    assert "GRIST_DOC_ID is set to the document 'Season 2025'. Set up that document? [Y/n]" in out
    assert "Which workspace" not in out and "Use workspace" not in out  # straight to the examples
    assert "Meets" in fake.tables and fake.created_docs == []
    assert DOC not in out


def test_saying_no_to_grist_doc_id_continues_with_the_workspace_questions(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "n\ny\nNew one\nn\nn\ny\n", env=ENV) == 0
    out = capsys.readouterr().out
    assert "Use workspace 'Work'?" in out and fake.created_docs == [(10, "New one")]
    assert "1. Update GRIST_DOC_ID in your settings" in out
    assert "from the old document to the new one" in out
    assert "GRIST_DOC_ID=NEWDOC" in out and "keep using the old document" in out


def test_without_grist_doc_id_the_next_step_is_just_to_add_it(monkeypatch, capsys):
    assert guided(monkeypatch, FakeGrist(), "y\nNew one\nn\nn\ny\n") == 0
    out = capsys.readouterr().out
    assert "Add this line to your settings" in out and "old document" not in out


def test_flag_form_create_with_grist_doc_id_set_also_warns_about_the_old_document(capsys):
    assert run(FakeGrist(), *CREATE, env=ENV) == 0
    assert "from the old document to the new one" in capsys.readouterr().out


def test_failure_after_create_says_grist_doc_id_still_points_at_the_old_document(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST /api/docs/NEWDOC/tables"
    assert run(fake, *CREATE, env=ENV) == 1
    err = capsys.readouterr().err
    assert "GRIST_DOC_ID=NEWDOC in your settings (it still points at the old document)" in err


def test_unreadable_grist_doc_id_gets_the_usual_readable_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Stdin("", tty=True))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    assert main([], ENV, reply(404, "not found")) == 1
    assert "could not find the document (HTTP 404)" in capsys.readouterr().err
    assert main([], ENV, reply(401, "no")) == 1
    assert "rejected the API key" in capsys.readouterr().err
