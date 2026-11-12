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
    assert "example Events" in out and "example Implements" in out
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
    assert "Create table Athletes" in out and "Plan:" in out


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
    assert "Create document" in capsys.readouterr().out


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
    assert "Done before the failure:" in err and "Created document" in err
    assert "Not done" in err and KEY not in err


def test_failure_without_create_has_no_doc_hint(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST"
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert "without --create" not in err and "GRIST_DOC_ID=" not in err
    assert "Fix the cause, then re-run" in err and "Not done" in err


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
    assert "--allow-existing-tables" in err and "Done before the failure" not in err
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
    assert "Plan:" in out and "Apply these changes" not in out and "done:" not in out
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
    assert "Plan:" in before
    assert "Create document 'BestYet' in workspace 10" in before
    assert "remove Grist's default table Table1" in before
    assert all(f"Create table {t.id}" in before for t in schema.TABLES)
    assert "Summary" not in before and "Created document" not in before

    capsys.readouterr()
    fake = FakeGrist()
    seen = spy_first_write(fake, capsys)
    assert run(fake) == 0
    assert "Plan:" in seen[0] and "Create table Athletes" in seen[0] and "Summary" not in seen[0]
    assert "Created table Athletes" in capsys.readouterr().out


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
    assert "pass --yes to run non-interactively" in captured.err
    assert captured.err.count("\n") == 1 and "Apply these changes" not in captured.out
    assert "Plan:" in captured.out
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
    assert "Plan:" in out and "Apply these changes" not in out and fake.writes == []
    assert run(fake, *CREATE, "--dry-run", env=ENV_NO_DOC, yes=False) == 0
    assert "Table1" in capsys.readouterr().out
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
    assert out.index("Add the example Events") < out.index("Add demo data")


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
    assert "Add demo data" in capsys.readouterr().out and fake.writes == []
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
    assert err.startswith("error: Grist reported a server error (HTTP 500: boom)")
    assert "The run stopped part-way." in err
    assert "Done before the failure:\n  - Created table Meets" in err
    assert "Not done (the first step failed):\n  - Add missing columns to Results: Mark" in err
    assert err.rstrip().endswith("re-run: it only adds what is missing.")
    assert "Summary" not in captured.out and KEY not in err + captured.out


def test_partial_failure_with_nothing_done(capsys):
    fake = full_fake()
    del fake.tables["Meets"]
    fake.fail_on = "POST"
    assert run(fake) == 1
    assert "Done before the failure: nothing" in capsys.readouterr().err


def test_partial_failure_after_create_names_the_document(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST /api/docs/NEWDOC/tables"
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    captured = capsys.readouterr()
    assert "Created document 'BestYet' (id NEWDOC)." in captured.out  # printed straight away
    assert "Done before the failure:\n  - Created document 'BestYet' (id NEWDOC)" in captured.err
    assert "Removed Grist's empty default table Table1" in captured.err
    assert "re-run without --create and with GRIST_DOC_ID=NEWDOC" in captured.err


def test_create_prints_the_closing_summary(capsys):
    fake = FakeGrist()
    assert run(fake, *CREATE, "--name", "My Doc", env=ENV_NO_DOC) == 0
    captured = capsys.readouterr()
    out = captured.out
    assert captured.err == "" and KEY not in out
    summary = out[out.index("\nSummary") :]
    assert "Created document 'My Doc' (id NEWDOC)" in summary
    assert "Removed Grist's empty default table Table1" in summary
    assert "Created table Athletes" in summary and "Created the 'Personal bests' page" in summary
    assert "Already there" not in summary and "Needs attention" not in summary
    assert "\n      GRIST_DOC_ID=NEWDOC\n" in summary  # on a line of its own
    assert "saved nothing" in summary and "bestyet-init --check" in summary


def test_rerun_summary_lists_what_was_added_and_what_was_there(capsys):
    fake = full_fake()
    del fake.tables["Meets"]
    assert run(fake) == 0
    out = capsys.readouterr().out
    summary = out[out.index("\nSummary") :]
    assert "Created table Meets" in summary and "Already there: tables Athletes" in summary
    assert "Meets," not in summary.split("Already there")[1]
    assert "GRIST_DOC_ID" not in summary and "bestyet-init --check" in summary


def test_normal_output_has_no_internal_notes(capsys):
    for argv, fake in (
        ([], FakeGrist()),
        (["--dry-run"], full_fake()),
        (["--demo-data"], FakeGrist()),
    ):
        assert run(fake, *argv) == 0
        out = capsys.readouterr().out
        for internal in (" yet", "TODO", "not handled", "not implemented", "colRef", "recalc"):
            assert internal not in out


def test_no_op_points_at_check(capsys):
    assert run(full_fake()) == 0
    out = capsys.readouterr().out
    assert out.startswith("Nothing to do") and "bestyet-init --check" in out
