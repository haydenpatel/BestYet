"""The init script's command line: options, exit codes, the confirmation prompt and interrupts."""

import sys

import pytest
from fake_grist import BASE, KEY, FakeGrist
from init_helpers import (
    CREATE,
    ENV,
    ENV_NO_DOC,
    Interrupting,
    Stdin,
    answers_from,
    full_fake,
    full_tables,
    intercept,
    rename_column,
    rename_table,
    run,
    source_files,
)

from bestyet import schema
from bestyet.init_grist import main


def test_script_has_no_environment_marker():
    for path in source_files():
        assert "TEST" not in path.read_text(), path.name


# Options.


@pytest.mark.parametrize("flag", ["--seed-implements", "--seed-events", "--demo-data", "--name=X"])
def test_write_options_are_rejected_with_check(capsys, flag):
    with pytest.raises(SystemExit):
        main(["--check", flag], ENV, FakeGrist().transport)
    assert flag.split("=")[0] in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--create"], ["--workspace", "Work"]])
def test_workspace_options_must_go_together(argv):
    with pytest.raises(SystemExit) as e:
        run(FakeGrist(), *argv)
    assert e.value.code == 2


def test_missing_settings_and_failing_calls_exit_nonzero(capsys):
    fake = FakeGrist()
    assert run(fake, env={"GRIST_URL": BASE}) == 1
    assert "GRIST_API_KEY is missing" in capsys.readouterr().err
    fake.fail_on = "/tables"
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert err.strip().startswith("error: Grist reported a server error (HTTP 500: boom)")
    assert err.count("\n") == 1


def test_an_unexpected_error_is_one_line_without_the_key(capsys):
    class Boom:
        def handle_request(self, request):
            raise RuntimeError(f"secret {KEY}")

    assert main([], ENV, Boom()) == 1
    err = capsys.readouterr().err
    assert KEY not in err and err.count("\n") == 1


def test_the_key_never_reaches_the_output(capsys):
    fake = FakeGrist({"Notes": {}})
    fake.fail_on = "POST /api/docs/DOC_ID_HERE/tables"
    run(fake)
    run(fake, "--allow-existing-tables", "--dry-run", yes=False)
    run(fake, "--allow-existing-tables")
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err
    assert "Traceback" not in captured.err


# Documents that cannot be set up.


@pytest.mark.parametrize("flags", [(), ("--seed-implements", "--demo-data")])
@pytest.mark.parametrize(
    ("rename", "spellings"),
    [(rename_column, ("'name'", "'Name'")), (rename_table, ("'meets'",))],
)
def test_case_only_clashes_are_refused_and_nothing_is_written(capsys, rename, spellings, flags):
    tables = full_tables()
    rename(tables)
    fake = full_fake(tables)
    assert run(fake, *flags) == 1
    captured = capsys.readouterr()
    assert "Fix them in Grist" in captured.err
    assert all(x in captured.out for x in spellings)
    assert "demo data" not in captured.out and "example Events" not in captured.out
    assert fake.writes == []


def test_a_page_without_its_summary_table_is_a_conflict(capsys):
    tables = full_tables()
    del tables[schema.SUMMARY.id]
    fake = FakeGrist(tables)
    fake.views[1] = schema.SUMMARY.page
    assert run(fake) == 1
    assert "exists but summary table" in capsys.readouterr().out
    assert fake.writes == []


def test_a_plain_table_with_the_summary_name_is_a_conflict():
    fake = full_fake()
    fake.summary_tables.clear()
    assert run(fake) == 1
    assert fake.writes == []


# Creating the document.


def test_create_by_workspace_id_or_name_and_custom_name(capsys):
    fake = FakeGrist()
    assert run(fake, "--create", "--workspace", "10", env=ENV_NO_DOC) == 0
    assert fake.created_docs == [(10, "BestYet")] and "NEWDOC" in capsys.readouterr().out
    fake = FakeGrist()
    assert run(fake, *CREATE, "--name", "My Doc", env=ENV_NO_DOC) == 0
    assert fake.created_docs == [(10, "My Doc")]


def test_an_ambiguous_workspace_needs_the_organisation(capsys):
    orgs = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    spaces = {1: [{"id": 10, "name": "Work"}], 2: [{"id": 20, "name": "Work"}]}
    fake = FakeGrist(orgs=orgs, workspaces=spaces)
    assert run(fake, *CREATE) == 1
    assert "ambiguous" in capsys.readouterr().err and fake.created_docs == []
    assert run(fake, *CREATE, "--org", "B") == 0
    assert fake.created_docs == [(20, "BestYet")]


def test_an_unknown_workspace_or_organisation_is_an_error(capsys):
    fake = FakeGrist()
    assert run(fake, "--create", "--workspace", "Nope") == 1
    assert "Workspace 'Nope' not found" in capsys.readouterr().err
    assert run(fake, *CREATE, "--org", "Nope") == 1
    assert "Organisation" in capsys.readouterr().err
    assert fake.created_docs == [] and fake.writes == []


def test_a_workspace_given_by_id_is_shown_by_name(capsys):
    assert run(FakeGrist(), "--create", "--workspace", "10", "--dry-run", env=ENV_NO_DOC) == 0
    assert "workspace 'Work'" in capsys.readouterr().out


# Dry runs and the confirmation prompt.


def test_a_dry_run_shows_the_plan_and_writes_nothing_even_with_yes(monkeypatch, capsys):
    answers_from(monkeypatch, "", tty=False)
    fake = FakeGrist()
    assert run(fake, "--dry-run", "--yes", yes=False) == 0
    assert run(fake, *CREATE, "-y", "--dry-run", env=ENV_NO_DOC, yes=False) == 0
    out = capsys.readouterr().out
    assert out.count("--- Plan ---") == 2 and "New document 'BestYet' in workspace" in out
    assert "Apply these changes" not in out and "Done." not in out
    assert fake.writes == [] and fake.created_docs == []


@pytest.mark.parametrize(
    ("answer", "applied"),
    [("y\n", True), ("YES\n", True), ("n\n", False), ("\n", False), ("maybe\n", False)],
)
def test_only_yes_applies_the_plan(monkeypatch, capsys, answer, applied):
    answers_from(monkeypatch, answer)
    fake = FakeGrist()
    assert run(fake, yes=False) == 0
    assert "Apply these changes? [y/N]" in capsys.readouterr().out
    assert bool(fake.writes) is applied and ("Athletes" in fake.tables) is applied


def test_declining_the_plan_for_a_new_document_creates_nothing(monkeypatch):
    answers_from(monkeypatch, "n\n")
    fake = FakeGrist()
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 0
    assert fake.created_docs == [] and fake.writes == []


def test_without_a_terminal_and_without_yes_it_refuses(monkeypatch, capsys):
    answers_from(monkeypatch, "y\n", tty=False)
    fake = FakeGrist()
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 1
    captured = capsys.readouterr()
    assert "pass --yes to apply" in captured.err and captured.err.count("\n") == 1
    assert "--- Plan ---" in captured.out and "Apply these changes" not in captured.out
    assert fake.created_docs == [] and fake.writes == []


def test_yes_and_nothing_to_do_never_prompt(monkeypatch, capsys):
    class NoRead(Stdin):
        def readline(self, *a):
            raise AssertionError("prompted")

    monkeypatch.setattr(sys, "stdin", NoRead())
    fake = FakeGrist()
    assert main(["-y"], ENV, fake.transport) == 0
    assert "Apply these changes" not in capsys.readouterr().out and "Athletes" in fake.tables
    assert run(full_fake(), yes=False) == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_end_of_input_at_the_prompt_aborts_with_nothing_changed(monkeypatch, capsys):
    answers_from(monkeypatch, "")
    fake = FakeGrist()
    assert run(fake, yes=False) == 130
    captured = capsys.readouterr()
    assert "[y/N] \n" in captured.out and "error: aborted; nothing was changed" in captured.err
    assert fake.writes == []


def test_ctrl_c_at_the_prompt_aborts_with_nothing_changed(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", Interrupting())
    fake = FakeGrist()
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 130
    assert capsys.readouterr().err == "error: aborted; nothing was changed\n"
    assert fake.created_docs == [] and fake.writes == []


def _interrupt_on_table_writes(fake):
    def hook(request):
        if request.method == "POST" and request.url.path.endswith("/tables"):
            raise KeyboardInterrupt

    intercept(fake, hook)


def test_ctrl_c_while_applying_says_how_to_continue(capsys):
    fake = FakeGrist()
    _interrupt_on_table_writes(fake)
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 130
    err = capsys.readouterr().err
    assert err.startswith("error: aborted.") and "GRIST_DOC_ID=NEWDOC" in err
    assert err.count("\n") == 1 and "Traceback" not in err
    fake = FakeGrist()
    _interrupt_on_table_writes(fake)
    assert run(fake) == 130
    assert capsys.readouterr().err == "error: aborted; re-run to add what is still missing\n"
