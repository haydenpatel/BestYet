import json

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


def run(fake, *argv, env=ENV):
    return main(list(argv), env, fake.transport)


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
    assert run(fake, "--dry-run") == 0
    assert fake.writes == []
    out = capsys.readouterr().out
    assert "would create table Athletes" in out


def test_create_in_workspace_by_id(capsys):
    fake = FakeGrist()
    env = {k: v for k, v in ENV.items() if k != "GRIST_DOC_ID"}
    assert run(fake, "--create", "--workspace", "10", env=env) == 0
    assert fake.created_docs == [(10, "BestYet")]
    assert "NEWDOC" in capsys.readouterr().out


def test_create_workspace_by_name_and_custom_name():
    fake = FakeGrist()
    assert run(fake, "--create", "--workspace", "Work", "--name", "My Doc") == 0
    assert fake.created_docs == [(10, "My Doc")]


def test_create_dry_run_creates_nothing(capsys):
    fake = FakeGrist()
    assert run(fake, "--create", "--workspace", "Work", "--dry-run") == 0
    assert fake.created_docs == [] and fake.writes == []
    assert "would create document" in capsys.readouterr().out


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
    run(fake, "--allow-existing-tables", "--dry-run")
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
