import io
import json
import sys

import httpx
import pytest
from fake_grist import BASE, DOC, KEY, FakeGrist

from bestyet import schema
from bestyet.config import Config
from bestyet.grist import GristClient
from bestyet.init_grist import build_plan, column_payload, main

ENV = {"GRIST_URL": BASE, "GRIST_DOC_ID": DOC, "GRIST_API_KEY": KEY}


def plain(table):
    return [c for c in table.columns if c.formula is None]


def full_tables():
    return {t.id: {c.id: {"type": c.type} for c in plain(t)} for t in schema.TABLES}


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


def test_fresh_doc_gets_all_tables_and_plain_columns(capsys):
    fake = FakeGrist()
    assert run(fake) == 0
    assert set(fake.tables) == {t.id for t in schema.TABLES}
    for t in schema.TABLES:
        assert set(fake.tables[t.id]) == {c.id for c in plain(t)}
    assert fake.tables["Results"]["Athlete"] == {"type": "Ref:Athletes"}
    assert fake.tables["Meets"]["Date"] == {"type": "Date"}
    out = capsys.readouterr().out
    assert "not handled by this script yet" in out


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
    assert "nothing to do" in capsys.readouterr().out


def test_partial_doc_gets_only_missing_parts(capsys):
    tables = full_tables()
    del tables["Meets"]
    del tables["Results"]["Mark"]
    tables["Athletes"]["Name"] = {"type": "Numeric"}  # wrong type: noted, not changed
    fake = FakeGrist(tables)
    assert run(fake) == 0
    assert set(fake.tables["Meets"]) == {"Name", "Date", "Location"}
    assert fake.tables["Results"]["Mark"] == {"type": "Text"}
    assert fake.tables["Athletes"]["Name"] == {"type": "Numeric"}
    posts = [w for w in fake.writes if w[0] == "POST"]
    assert len(posts) == 2
    assert "left as is" in capsys.readouterr().out


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
    fake = FakeGrist(full_tables())
    fake.tables[schema.SUMMARY.id] = {"count": {"type": "Int"}}
    fake.summary_tables.add(schema.SUMMARY.id)
    assert run(fake) == 0
    assert fake.writes == []


def test_dry_run_changes_nothing(capsys):
    fake = FakeGrist()
    assert run(fake, "--dry-run", yes=False) == 0
    assert fake.writes == []
    out = capsys.readouterr().out
    assert "create table Athletes" in out and "plan:" in out


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
    assert "create document" in capsys.readouterr().out


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
    assert "organisation" in capsys.readouterr().err


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
    assert err.strip().startswith("error: Grist error 500: boom")
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


def test_plan_skips_formula_columns():
    steps = build_plan({}).steps
    results = next(s for s in steps if s.table.id == "Results")
    assert {c.id for c in results.columns} == {"Athlete", "Event", "Meet", "Mark", "Place"}
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
    fake = FakeGrist(tables)
    assert run(fake) == 1
    captured = capsys.readouterr()
    assert "'name'" in captured.out and "'Name'" in captured.out
    assert "fix it in Grist" in captured.err
    assert fake.writes == []
    assert set(fake.tables["Athletes"]) == {"name", "Grade"}


def test_case_only_table_conflict_warns_and_fails(capsys):
    tables = full_tables()
    tables["meets"] = tables.pop("Meets")
    fake = FakeGrist(tables)
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
    assert err.count("\n") == 1


def test_failure_without_create_has_no_doc_hint(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST"
    assert run(fake) == 1
    assert "re-run" not in capsys.readouterr().err


def test_unrelated_tables_columns_not_read():
    fake = FakeGrist({"Notes": {}})
    fake.fail_on = "tables/Notes/columns"
    assert run(fake, "--allow-existing-tables") == 0


# Grist's default table, the plan, and the confirmation prompt.


def test_create_then_rerun_is_a_noop(capsys):
    fake = FakeGrist()
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    out = capsys.readouterr().out
    assert "document id: NEWDOC" in out and "warning" not in out
    assert "Table1" not in fake.tables and fake.actions == [["RemoveTable", "Table1"]]
    assert set(fake.tables) == {t.id for t in schema.TABLES}
    fake.writes.clear()
    assert run(fake) == 0
    assert fake.writes == []
    assert "nothing to do" in capsys.readouterr().out


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
    assert "Table1" in fake.tables and fake.actions == []
    assert "Athletes" in fake.tables
    warning = f"warning: Table1 not removed: {reason}"
    assert out.count(warning) == 2  # when found, and again in the summary
    summary = out[out.index("not handled by this script yet") :]
    assert warning in summary and "--allow-existing-tables" in summary
    assert "remove Table1 in Grist" in summary


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
    assert "Table1" in fake.tables and fake.actions == []
    assert f"Table1 not removed: {reason}" in capsys.readouterr().out


def test_remove_table_failure_reports_id_and_hint(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST /api/docs/NEWDOC/apply"
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    err = capsys.readouterr().err
    assert "NEWDOC" in err and "GRIST_DOC_ID=NEWDOC" in err
    assert "--allow-existing-tables" in err and err.count("\n") == 1
    assert "Table1" in fake.tables


def test_next_step_depends_on_the_allow_flag(capsys):
    fake = FakeGrist({"Table1": DEFAULTS})
    assert run(fake, "--allow-existing-tables") == 0
    summary = capsys.readouterr().out.split("not handled by this script yet")[1]
    assert "next step: remove Table1 in Grist" in summary
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
    assert "[y/N] \nnot applied" in capsys.readouterr().out


def test_dry_run_wins_over_yes(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Stdin("", tty=False))
    fake = FakeGrist()
    assert run(fake, "--yes", "--dry-run", yes=False) == 0
    assert run(fake, *CREATE, "-y", "--dry-run", env=ENV_NO_DOC, yes=False) == 0
    out = capsys.readouterr().out
    assert "plan:" in out and "Apply these changes" not in out and "done:" not in out
    assert fake.writes == [] and fake.created_docs == []


def test_existing_document_keeps_default_table_and_warns(capsys):
    fake = FakeGrist({"Table1": DEFAULTS})
    assert run(fake, "--allow-existing-tables") == 0
    out = capsys.readouterr().out
    assert "Table1" in fake.tables and fake.actions == []
    warning = "warning: Table1 not removed: the document was not created by this run"
    assert out.count(warning) == 2 and "next step" in out


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
    assert "plan:" in before
    assert "create document 'BestYet' in workspace 10" in before
    assert "remove it" in before and "Table1" in before
    assert all(f"create table {t.id}" in before for t in schema.TABLES)
    assert "done:" not in before and "document id" not in before

    capsys.readouterr()
    fake = FakeGrist()
    seen = spy_first_write(fake, capsys)
    assert run(fake) == 0
    assert "plan:" in seen[0] and "create table Athletes" in seen[0] and "done:" not in seen[0]
    assert "done: create table Athletes" in capsys.readouterr().out


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
    assert "plan:" in captured.out
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
    assert "plan:" in out and "Apply these changes" not in out and fake.writes == []
    assert run(fake, *CREATE, "--dry-run", env=ENV_NO_DOC, yes=False) == 0
    assert "Table1" in capsys.readouterr().out
    assert fake.created_docs == [] and fake.writes == []
    done = FakeGrist(full_tables())
    assert run(done, yes=False) == 0
    assert "nothing to do" in capsys.readouterr().out
