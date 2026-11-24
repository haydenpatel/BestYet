"""The guided mode: which questions are asked, what the answers do, and when it does not start.

Answers are scripted in the order asked, e.g. "y\\nDoc\\nn\\nn\\nn\\ny\\n" is workspace, name, no demo
data, no events, no implements, apply.
"""

import sys

import httpx
import pytest
from fake_grist import DOC, KEY, FakeGrist
from init_helpers import (
    CREATE,
    DISCUS_SHOT,
    ENV,
    ENV_NO_DOC,
    N_IMPLEMENTS,
    Interrupting,
    before_nth_get,
    full_fake,
    full_tables,
    guided,
    incomplete_fake,
    intercept,
    reply,
    run,
    terminal,
    workspace_with,
)

from bestyet import schema
from bestyet.init_grist import main
from bestyet.init_plan import implement_event_names

QUALIFIER = f"This also adds the {DISCUS_SHOT} events, which the implements use."
PROMPT = "Add sample implement weights (by grade and division)?"
NEW_DOC = "y\nDoc\nn\nn\nn\ny\n"  # the workspace, a name, nothing pre-loaded, apply
TWO_ORGS = {
    "orgs": [{"id": 1, "name": "Org One"}, {"id": 2, "name": "Org Two"}],
    "workspaces": {1: [{"id": 10, "name": "Work"}], 2: [{"id": 20, "name": "Home"}]},
}


def stopped(fake, capsys):
    """Nothing was created or written, and the run said so on stderr."""
    assert fake.created_docs == [] and fake.writes == []
    return capsys.readouterr().err


# Whether the guide starts.


def test_a_terminal_without_options_starts_the_guide(monkeypatch):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, NEW_DOC) == 0
    assert fake.created_docs == [(10, "Doc")]


@pytest.mark.parametrize("argv", [["--check"], ["--dry-run"], ["--yes"], ["--demo-data"]])
def test_options_that_say_enough_never_start_the_guide(monkeypatch, capsys, argv):
    terminal(monkeypatch)
    main(argv, ENV, full_fake().transport)
    assert "BestYet setup" not in capsys.readouterr().out


def test_fully_specified_options_in_a_terminal_ask_nothing(monkeypatch, capsys):
    fake = FakeGrist()
    terminal(monkeypatch)
    assert main([*CREATE, "--yes"], ENV_NO_DOC, fake.transport) == 0
    assert "BestYet setup" not in capsys.readouterr().out
    assert fake.created_docs == [(10, "BestYet")]


def test_without_a_terminal_it_never_prompts(monkeypatch, capsys):
    terminal(monkeypatch, "1\n", tty=False)
    fake = incomplete_fake()
    assert main([], ENV, fake.transport) == 1
    captured = capsys.readouterr()
    assert "BestYet setup" not in captured.out and "without options" in captured.err
    assert fake.writes == []


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--create"], "--create needs --workspace"),
        (["--workspace", "Work"], "only apply with --create"),
        (["--org", "Org One"], "only apply with --create"),
    ],
)
def test_missing_values_are_usage_errors_without_a_terminal(monkeypatch, capsys, argv, message):
    terminal(monkeypatch, "y\n", tty=False)
    with pytest.raises(SystemExit):
        main(argv, ENV, FakeGrist().transport)
    assert message in capsys.readouterr().err


def test_create_without_a_workspace_asks_for_it_in_a_terminal(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, NEW_DOC, "--create") == 0
    assert "Use workspace 'Work'?" in capsys.readouterr().out
    assert fake.created_docs == [(10, "Doc")]


@pytest.mark.parametrize("flag", ["--yes", "--dry-run"])
def test_with_yes_or_dry_run_only_the_missing_value_is_asked(monkeypatch, capsys, flag):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "y\nDoc\ny\n", "--create", flag) == 0
    out = capsys.readouterr().out
    assert "Use workspace 'Work'?" in out and "Name for the new document" in out
    assert "Add demo data" not in out and "Add default events" not in out and PROMPT not in out
    if flag == "--yes":
        assert fake.created_docs == [(10, "Doc")]
        assert not fake.rows.get("Events") and not fake.rows.get("Implements")
    else:
        assert "Dry run" in out and "Apply these changes" not in out
        assert fake.created_docs == [] and fake.writes == []


@pytest.mark.parametrize("answers", ["", "y\n", "y\nc\n", "y\nc\nDoc\nn\n"])
def test_end_of_input_stops_cleanly(monkeypatch, capsys, answers):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, answers) == 130
    assert "error: aborted; nothing was changed" in stopped(fake, capsys)


def test_ctrl_c_stops_cleanly(monkeypatch, capsys):
    fake = FakeGrist()
    monkeypatch.setattr(sys, "stdin", Interrupting())
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    assert main([], ENV, fake.transport) == 130
    assert "aborted; nothing was changed" in stopped(fake, capsys)


# Choosing the workspace.


def test_a_lone_workspace_is_confirmed_not_assumed(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "n\n") == 0
    out = capsys.readouterr().out
    assert "Use workspace 'Work'? [Y/n]" in out
    assert "Create one in Grist, then run this again. Nothing was changed." in out
    stopped(fake, capsys)


def test_several_workspaces_are_listed_with_their_organisation_when_there_are_several(
    monkeypatch, capsys
):
    fake = FakeGrist(**TWO_ORGS)
    assert guided(monkeypatch, fake, "2\nDoc\nn\nn\nn\ny\n") == 0
    assert "2. Home (Org Two)" in capsys.readouterr().out
    assert fake.created_docs == [(20, "Doc")]
    fake = FakeGrist(workspaces={1: [{"id": 10, "name": "Work"}, {"id": 11, "name": "Home"}]})
    assert guided(monkeypatch, fake, "2\nDoc\nn\nn\nn\ny\n") == 0
    out = capsys.readouterr().out
    assert "2. Home\n" in out and "Use workspace" not in out and "(Org" not in out
    assert fake.created_docs == [(11, "Doc")]


def test_org_option_narrows_the_workspaces_and_must_exist(monkeypatch, capsys):
    fake = FakeGrist(**TWO_ORGS)
    assert guided(monkeypatch, fake, NEW_DOC, "--org", "Org Two") == 0
    out = capsys.readouterr().out
    assert "Use workspace 'Home'?" in out and "Work" not in out
    assert fake.created_docs == [(20, "Doc")]
    assert guided(monkeypatch, FakeGrist(), "", "--org", "Nope") == 1
    err = capsys.readouterr().err
    assert "Organisation 'Nope' not found" in err and "cannot see any workspace" not in err


def test_no_visible_workspace_is_an_error(monkeypatch, capsys):
    assert guided(monkeypatch, FakeGrist(workspaces={1: []}), "") == 1
    assert "cannot see any workspace" in capsys.readouterr().err


def test_bad_answers_are_asked_again(monkeypatch, capsys):
    fake = FakeGrist(workspaces=workspace_with("Alpha"))
    assert guided(monkeypatch, fake, "x\ny\n0\n7\nc\nDoc\nn\nmaybe\nn\nn\ny\n") == 0
    out = capsys.readouterr().out
    assert "Please enter a number from 1 to 1, or C." in out and "Please answer y or n." in out
    assert fake.created_docs == [(10, "Doc")]


def test_workspace_option_skips_only_the_workspace_question(monkeypatch, capsys):
    fake = FakeGrist(workspaces=workspace_with("Alpha"))
    assert guided(monkeypatch, fake, "c\nDoc\nn\nn\nn\ny\n", "--workspace", "Work") == 0
    out = capsys.readouterr().out
    assert "Which workspace" not in out
    assert "Do you want to use an existing document in workspace 'Work'?" in out
    assert fake.created_docs == [(10, "Doc")]


def test_the_workspace_and_its_documents_are_looked_up_once():
    fake = FakeGrist(workspaces=workspace_with("Alpha"))
    listings = []
    intercept(fake, lambda r: listings.append(1) if r.url.path.endswith("/workspaces") else None)
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    assert len(listings) == 3  # the choice, the documents, the re-check before creating


# Choosing or creating the document.


def test_new_document_with_every_answer_given(monkeypatch, capsys):
    fake = FakeGrist()
    answers = "y\nMy Doc\nn\ny\ny\ny\n"  # workspace, name, no demo, events, implements, apply
    assert guided(monkeypatch, fake, answers) == 0
    out = capsys.readouterr().out
    assert fake.created_docs == [(10, "My Doc")]
    assert len(fake.rows["Implements"]) == N_IMPLEMENTS
    assert len(fake.rows["Events"]) == len(schema.EXAMPLE_EVENTS) and "Athletes" not in fake.rows
    assert "New document 'My Doc' in workspace 'Work'" in out and "GRIST_DOC_ID=NEWDOC" in out
    assert KEY not in out


def test_declining_the_plan_changes_nothing(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "y\n\nn\nn\nn\nn\n") == 0
    assert "Not applied" in capsys.readouterr().out
    assert fake.created_docs == [] and fake.writes == []


def test_the_default_name_is_used_and_demo_data_skips_the_other_questions(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "y\n\ny\ny\n") == 0  # workspace, name, demo yes, apply
    out = capsys.readouterr().out
    assert fake.created_docs == [(10, "BestYet")]
    assert len(fake.rows["Results"]) == len(schema.DEMO_RESULTS)
    assert len(fake.rows["Implements"]) == N_IMPLEMENTS  # the demo needs them
    assert "Add default events" not in out and PROMPT not in out


def test_flags_skip_their_questions(monkeypatch, capsys):
    fake = FakeGrist()
    argv = ("--create", "--name", "Given", "--seed-implements")
    assert guided(monkeypatch, fake, "y\nn\nn\ny\n", *argv) == 0  # workspace, demo, events, apply
    out = capsys.readouterr().out
    assert "Name for the new" not in out and PROMPT not in out
    assert "Add demo data" in out and "Add default events" in out
    assert fake.created_docs == [(10, "Given")]


def test_the_documents_in_the_workspace_are_listed_and_the_chosen_one_is_set_up(
    monkeypatch, capsys
):
    fake = incomplete_fake()
    fake.workspaces = workspace_with("Alpha", "Beta")
    assert guided(monkeypatch, fake, "y\n2\nn\nn\nn\ny\n") == 0  # Beta, nothing extra
    out = capsys.readouterr().out
    assert "1. Alpha" in out and "2. Beta" in out and "or C to create a new document:" in out
    assert "Meets" in fake.tables and fake.created_docs == []
    assert any("/api/docs/DOC1/" in path for _, path in fake.writes)


def test_grist_doc_id_is_offered_first_by_name(monkeypatch, capsys):
    fake = incomplete_fake()
    assert guided(monkeypatch, fake, "y\nn\nn\nn\ny\n", env=ENV) == 0
    out = capsys.readouterr().out
    assert "GRIST_DOC_ID is set to the document 'Season 2025'. Set up that document? [Y/n]" in out
    assert "Which workspace" not in out and "Use workspace" not in out and DOC not in out
    assert "Meets" in fake.tables and fake.created_docs == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [(404, "could not find the document (HTTP 404)"), (403, "denied access (HTTP 403)")],
)
def test_an_unreadable_grist_doc_id_does_not_stop_the_questions(
    monkeypatch, capsys, status, reason
):
    fake = FakeGrist()
    intercept(
        fake,
        lambda r: (
            httpx.Response(status, json={"error": "no"})
            if r.method == "GET" and r.url.path == f"/api/docs/{DOC}"
            else None
        ),
    )
    assert guided(monkeypatch, fake, NEW_DOC, env=ENV) == 0
    out = capsys.readouterr().out
    assert "GRIST_DOC_ID is set but that document cannot be read." in out and reason in out
    assert "Choose another document below." in out
    assert "Choose another document below." in out
    assert "Set up that document?" not in out and "Use workspace 'Work'?" in out
    assert fake.created_docs == [(10, "Doc")]


def test_an_unreachable_server_still_gives_the_usual_error(monkeypatch, capsys):
    terminal(monkeypatch)
    assert main([], ENV, reply(401, "no")) == 1
    assert "rejected the API key" in capsys.readouterr().err


def test_an_existing_document_with_other_tables_asks_before_anything_else(monkeypatch, capsys):
    tables = {**full_tables(), "Notes": {"A": {"type": "Text"}}}
    fake = full_fake(tables)
    del fake.tables["Meets"]
    assert guided(monkeypatch, fake, "y\ny\nn\nn\nn\ny\n", env=ENV) == 0
    out = capsys.readouterr().out
    assert "not part of BestYet (Notes)" in out and out.index("Notes") < out.index(
        "sample implements"
    )
    assert "Meets" in fake.tables
    fake = full_fake(tables)
    assert guided(monkeypatch, fake, "y\nn\n", env=ENV) == 0
    assert "Nothing was changed" in capsys.readouterr().out and fake.writes == []


# A name that is already taken in the workspace.


def clashing(*names, **kwargs):
    return FakeGrist(workspaces=workspace_with(*names), **kwargs)


def test_a_taken_name_can_be_replaced_by_another(monkeypatch, capsys):
    fake = clashing("My Doc")
    answers = "y\nc\n  my doc \nn\nOther\nn\nn\nn\ny\n"  # clash, don't use it, another name, ...
    assert guided(monkeypatch, fake, answers) == 0
    out = capsys.readouterr().out
    assert out.count("A document named 'My Doc' already exists in this workspace.") == 1
    assert "Use it instead? [Y/n]" in out
    assert fake.created_docs == [(10, "Other")]


def test_a_taken_name_can_be_used_as_the_document_instead(monkeypatch):
    fake = clashing("My Doc")
    assert guided(monkeypatch, fake, "y\nc\nmy doc\n\nn\nn\nn\ny\n") == 0
    assert fake.created_docs == []  # never a second document
    assert set(fake.tables) >= {t.id for t in schema.TABLES}
    assert all("/api/docs/DOC0/" in path for _, path in fake.writes)


def test_a_given_name_that_is_taken_stops_on_no(monkeypatch, capsys):
    fake = clashing("My Doc")
    assert guided(monkeypatch, fake, "y\nn\n", "--create", "--name", "my doc") == 0
    assert "Nothing was changed" in capsys.readouterr().out
    assert fake.created_docs == [] and fake.writes == []


def test_the_same_name_in_another_workspace_is_not_a_clash():
    docs = [{"id": "D", "name": "BestYet"}]
    fake = FakeGrist(
        workspaces={
            1: [{"id": 10, "name": "Work", "docs": docs}, {"id": 11, "name": "Home", "docs": []}]
        }
    )
    assert run(fake, "--create", "--workspace", "Home", env=ENV_NO_DOC) == 0
    assert fake.created_docs == [(11, "BestYet")]


@pytest.mark.parametrize("name", ["BestYet", "bestyet", "  BESTYET  "])
@pytest.mark.parametrize("with_yes", [False, True])
def test_flags_refuse_a_taken_name_when_they_cannot_ask(monkeypatch, capsys, name, with_yes):
    fake = clashing("BestYet")
    argv = ("--create", "--workspace", "Work", "--name", name)
    if with_yes:  # --yes means there is nobody to ask
        assert run(fake, *argv, env=ENV_NO_DOC) == 1
    else:
        terminal(monkeypatch, "y\n", tty=False)
        assert run(fake, *argv, env=ENV_NO_DOC, yes=False) == 1
    err = stopped(fake, capsys)
    assert "A document named 'BestYet' already exists in workspace 'Work'" in err
    assert "--name" in err and "DOC0" not in err


def test_flags_in_a_terminal_ask_whether_to_use_the_taken_name(monkeypatch, capsys):
    fake = clashing("BestYet")
    terminal(monkeypatch, "n\n")
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 0
    out = capsys.readouterr().out
    assert "Use it instead? [Y/n]" in out and "Nothing was changed" in out
    assert fake.created_docs == [] and fake.writes == []
    terminal(monkeypatch, "y\ny\n")  # use it, apply
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 0
    assert fake.created_docs == [] and fake.writes
    assert all("/api/docs/DOC0/" in path for _, path in fake.writes)
    fake = clashing("BestYet")
    terminal(monkeypatch, "")  # end of input at the question
    assert run(fake, *CREATE, env=ENV_NO_DOC, yes=False) == 130
    stopped(fake, capsys)


def test_a_dry_run_shows_the_plan_for_the_taken_name_but_not_with_yes(monkeypatch, capsys):
    fake = clashing("BestYet")
    terminal(monkeypatch)
    assert run(fake, *CREATE, "--dry-run", env=ENV_NO_DOC, yes=False) == 0
    out = capsys.readouterr().out
    assert "asks whether to use it" in out and "New document 'BestYet'" not in out
    assert "--- Plan ---" in out
    assert fake.created_docs == [] and fake.writes == []
    assert run(fake, *CREATE, "--dry-run", env=ENV_NO_DOC) == 1  # --yes: no switching unseen
    assert "already exists" in stopped(fake, capsys)


def test_the_name_is_checked_again_right_before_creating(capsys):
    fake = FakeGrist()
    # Listings of workspaces: the choice, the documents, then the check before creating.
    before_nth_get(
        fake, "/workspaces", 3, lambda: setattr(fake, "workspaces", workspace_with("BestYet"))
    )
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    assert "already exists" in capsys.readouterr().err and fake.created_docs == []


def test_documents_are_read_from_the_workspace_when_the_listing_has_none(capsys):
    fake = FakeGrist(workspaces={1: [{"id": 10, "name": "Work"}]})  # no "docs" in the listing
    docs = {"id": 10, "docs": [{"id": "D", "name": "BestYet"}]}
    intercept(
        fake,
        lambda r: httpx.Response(200, json=docs) if r.url.path == "/api/workspaces/10" else None,
    )
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    assert "already exists" in capsys.readouterr().err


def test_existing_document_runs_do_not_look_at_workspaces():
    fake = full_fake()
    fake.fail_on = "workspaces"
    assert run(fake) == 0


# The pre-loaded data questions.


def test_declining_everything_leaves_the_tables_empty(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, NEW_DOC) == 0
    out = capsys.readouterr().out
    assert "Add default events (standard track and field list)? [y/N]" in out
    assert f"{PROMPT}\n{QUALIFIER} [y/N]" in out  # the note follows the question, before the answer
    assert out.index(QUALIFIER) < out.index("--- Plan ---")
    assert "pre-loaded data" not in out.lower()
    assert not fake.rows.get("Events") and not fake.rows.get("Implements")


def test_implements_without_events_add_only_the_events_they_use(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "y\nDoc\nn\nn\ny\ny\n") == 0  # events no, implements yes
    out = capsys.readouterr().out
    assert f"Add 2 events required for the sample implements ({DISCUS_SHOT})" in out
    assert {r["fields"]["Name"] for r in fake.rows["Events"]} == set(implement_event_names())
    assert len(fake.rows["Implements"]) == N_IMPLEMENTS


def test_events_without_implements_add_only_the_events(monkeypatch):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "y\nDoc\nn\ny\nn\ny\n") == 0
    assert len(fake.rows["Events"]) == len(schema.EXAMPLE_EVENTS)
    assert not fake.rows.get("Implements")


def _without_discus():
    fake = full_fake()
    fake.rows["Events"] = [r for r in fake.rows["Events"] if r["fields"]["Name"] != "Discus"]
    return fake


@pytest.mark.parametrize(
    ("build", "answers", "argv", "env", "note"),
    [
        (FakeGrist, "y\nDoc\nn\nn\ny\ny\n", (), ENV_NO_DOC, True),  # yes would add events
        (_without_discus, "y\nn\nn\ny\ny\n", (), ENV, True),  # an existing document lacks them
        (FakeGrist, "y\nDoc\nn\ny\ny\ny\n", (), ENV_NO_DOC, False),  # events are added anyway
        (full_fake, "y\nn\nn\nn\n", (), ENV, False),  # they are already there
        (FakeGrist, "y\nDoc\ny\ny\n", (), ENV_NO_DOC, False),  # demo data covers them
    ],
)
def test_the_implements_note_is_shown_only_when_yes_would_add_events(
    monkeypatch, capsys, build, answers, argv, env, note
):
    assert guided(monkeypatch, build(), answers, *argv, env=env) == 0
    out = capsys.readouterr().out
    assert (QUALIFIER in out) is note
    if not note:
        assert "This also adds" not in out


def test_demo_data_is_not_offered_when_the_tables_have_rows(monkeypatch, capsys):
    fake = full_fake()
    fake.rows["Athletes"] = [{"id": 1, "fields": {"Name": "A"}}]
    assert guided(monkeypatch, fake, "y\nn\nn\n", env=ENV) == 0
    out = capsys.readouterr().out
    assert "Add demo data" not in out
    assert "Add default events" in out and "Add sample implement weights" in out


# Which document gets the settings step: any that is not the one GRIST_DOC_ID points at.


def settings_step_in(out, doc_id):
    return f"\n     GRIST_DOC_ID={doc_id}\n" in out


def two_documents(*, complete=False):
    fake = full_fake() if complete else incomplete_fake()
    fake.workspaces = workspace_with("Alpha", "Beta")
    return fake


def test_a_picked_document_gets_the_settings_step(monkeypatch, capsys):
    # GRIST_DOC_ID points elsewhere: say no to it, pick Beta.
    assert guided(monkeypatch, two_documents(), "n\ny\n2\nn\nn\nn\ny\n", env=ENV) == 0
    out = capsys.readouterr().out
    assert settings_step_in(out, "DOC1") and "currently points at a different document" in out
    assert "keep using that other document" in out and "3. Then run bestyet-init --check" in out
    # Without GRIST_DOC_ID the step is just to add it.
    assert guided(monkeypatch, two_documents(), "y\n2\nn\nn\nn\ny\n") == 0
    out = capsys.readouterr().out
    assert settings_step_in(out, "DOC1") and "Add this line to your settings" in out
    assert "old document" not in out


def test_a_complete_picked_document_still_prints_the_settings_step(monkeypatch, capsys):
    assert guided(monkeypatch, two_documents(complete=True), "y\n2\nn\nn\nn\n") == 0
    out = capsys.readouterr().out
    assert "Nothing to do" in out and settings_step_in(out, "DOC1")
    assert "Then run bestyet-init --check" not in out
    assert out.index("Nothing to do") < out.index("--- Next steps ---")


def test_the_document_in_grist_doc_id_needs_no_settings_step(monkeypatch, capsys):
    assert guided(monkeypatch, incomplete_fake(), "y\nn\nn\nn\ny\n", env=ENV) == 0  # yes: that one
    out = capsys.readouterr().out
    assert "GRIST_DOC_ID=" not in out and "2. Then run bestyet-init --check" in out
    fake = full_fake()
    fake.workspaces = workspace_with("Alpha")
    env = {**ENV, "GRIST_DOC_ID": "DOC0"}  # no, then pick that very document from the list
    assert guided(monkeypatch, fake, "n\ny\n1\nn\nn\nn\n", env=env) == 0
    assert "GRIST_DOC_ID=DOC0" not in capsys.readouterr().out


def test_a_taken_name_used_as_the_document_gets_the_settings_step(monkeypatch, capsys):
    fake = incomplete_fake()
    fake.workspaces = workspace_with("My Doc")
    assert guided(monkeypatch, fake, "y\nc\nmy doc\ny\nn\nn\nn\ny\n") == 0
    assert fake.created_docs == [] and settings_step_in(capsys.readouterr().out, "DOC0")
    fake = incomplete_fake()
    fake.workspaces = workspace_with("BestYet")
    terminal(monkeypatch, "y\ny\n")
    assert run(fake, *CREATE, env=ENV, yes=False) == 0  # the flag form asks the same
    out = capsys.readouterr().out
    assert fake.created_docs == [] and settings_step_in(out, "DOC0")
    assert "currently points at a different document" in out


def test_saying_no_to_grist_doc_id_continues_with_the_workspace_questions(monkeypatch, capsys):
    fake = FakeGrist()
    assert guided(monkeypatch, fake, "n\ny\nNew one\nn\nn\nn\ny\n", env=ENV) == 0
    out = capsys.readouterr().out
    assert "Use workspace 'Work'?" in out and fake.created_docs == [(10, "New one")]
    assert "1. Update GRIST_DOC_ID in your settings" in out and "GRIST_DOC_ID=NEWDOC" in out


def test_a_picked_document_that_fails_midway_gets_the_settings_item(monkeypatch, capsys):
    fake = two_documents()
    fake.fail_on = "POST /api/docs/DOC1/tables"
    assert guided(monkeypatch, fake, "y\n2\nn\nn\nn\ny\n") == 1
    err = capsys.readouterr().err
    assert "Add this line to your settings (environment or .env): GRIST_DOC_ID=DOC1" in err
    assert "3. Then run bestyet-init again; it only adds what is missing." in err
