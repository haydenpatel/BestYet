"""The words the init script prints: the plan, Done, next steps and failure reports.

Only the messages that matter to the person running it are pinned here; behaviour (what is
written, exit codes, which questions are asked) is tested next to the code that does it.
"""

import pytest
from fake_grist import KEY, FakeGrist
from init_helpers import (
    CREATE,
    DEFAULTS,
    ENV,
    ENV_NO_DOC,
    full_fake,
    implement_rows,
    incomplete_fake,
    intercept,
    run,
    server_error,
)

from bestyet import schema
from bestyet.init_output import done_message
from bestyet.init_steps import AddColumns, CreateTable, SeedEvents

DISCUS_SHOT = "Discus and Shot Put"
N_EVENTS = len(schema.EXAMPLE_EVENTS)
N_IMPLEMENTS = len(schema.SAMPLE_IMPLEMENTS)
HINT = "Add implement weights by hand"
FAIL_TABLES = "POST /api/docs/NEWDOC/tables"


def CT(name):
    return CreateTable(schema.table(name), ())


def columns(table, n=1):
    return AddColumns(table, tuple(schema.table(table).columns[:n]))


@pytest.mark.parametrize(
    ("steps", "doc", "expected"),
    [
        ([CT("Meets")], None, "Done. Added the table Meets."),
        ([CT("Meets"), CT("Results")], None, "Done. Added the tables Meets and Results."),
        (
            [CT("Athletes"), CT("Events"), CT("Meets")],
            None,
            "Done. Added the tables Athletes, Events and Meets.",
        ),
        (
            [CT("Athletes")],
            ("X", "workspace 'W'"),
            "Done. Created 'X' in workspace 'W' with 1 table.",
        ),
        (
            [CT("Athletes"), CT("Events")],
            ("X", "workspace 'W'"),
            "Done. Created 'X' in workspace 'W' with 2 tables.",
        ),
        ([columns("Results")], None, "Done. Added missing columns in the table Results."),
        (
            [columns("Results"), columns("Athletes"), SeedEvents()],
            None,
            "Done. Added missing columns in the tables Results and Athletes and pre-loaded data.",
        ),
    ],
)
def test_done_message(steps, doc, expected):
    assert done_message(steps, doc) == expected


# The plan.


def test_plan_for_a_new_document_is_grouped_in_plain_words(capsys):
    argv = (*CREATE, "--seed-implements", "--demo-data", "--dry-run")
    assert run(FakeGrist(), *argv, env=ENV_NO_DOC) == 0
    out = capsys.readouterr().out
    headings = [
        x.strip() for x in out.splitlines() if x.startswith("  ") and not x.startswith("    ")
    ]
    assert headings == ["New document 'BestYet' in workspace 'Work'", "Pre-loaded data"]
    assert "    - Tables: Athletes, Events, Meets, Implements, Results\n" in out
    assert "    - A 'Personal Bests' page showing each athlete's best per event\n" in out
    assert "Add demo data: 3 athletes, 2 meets, 7 results\n" in out
    for hidden in ("Calculated", "Display", "Table1", "Results_summary", "colRef"):
        assert hidden not in out  # behind the scenes: done, not announced


def test_plan_for_an_existing_document_names_only_what_is_missing(capsys):
    assert run(incomplete_fake(), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "  Tables\n    - Create Meets\n" in out and "New document" not in out
    assert run(incomplete_fake(table="Results", column="Mark"), "--dry-run") == 0
    assert "  Tables\n    - Add missing columns to Results: Mark\n" in capsys.readouterr().out


def _missing(*names):
    def build():
        fake = full_fake()
        fake.rows["Events"] = [r for r in fake.rows["Events"] if r["fields"]["Name"] not in names]
        return fake

    return build


def _implements_but_one():
    fake = full_fake()
    fake.rows["Implements"] = implement_rows(fake, schema.SAMPLE_IMPLEMENTS[:-1])
    return fake


@pytest.mark.parametrize(
    ("build", "argv", "lines", "absent"),
    [
        (
            FakeGrist,
            ("--seed-implements",),
            [f"Add 2 events required for the sample implements ({DISCUS_SHOT})"]
            + [f"Add the {N_IMPLEMENTS} sample implements"],
            [f"Add the {N_EVENTS} default events"],
        ),
        (
            _missing(*[n for n, _ in schema.EXAMPLE_EVENTS[-5:]]),
            ("--seed-events",),
            [f"Add 5 default events ({N_EVENTS - 5} already there)"],
            [],
        ),
        (
            _implements_but_one,
            ("--seed-implements",),
            [f"Add 1 sample implement ({N_IMPLEMENTS - 1} already there)"],
            [],
        ),
        (
            _missing("Discus"),
            ("--seed-implements",),
            ["Add 1 event required for the sample implements (Discus; 1 already there)"],
            [],
        ),
        (
            FakeGrist,
            ("--demo-data",),
            ["events required for the demo data and sample implements ("],
            ["default events"],
        ),
        (
            FakeGrist,
            ("--seed-events", "--demo-data"),
            [f"Add the {N_EVENTS} default events"],
            ["required for"],
        ),
    ],
)
def test_plan_counts_what_it_adds_and_what_is_already_there(capsys, build, argv, lines, absent):
    assert run(build(), *argv, "--dry-run") == 0
    out = capsys.readouterr().out
    assert all(line in out for line in lines)
    assert not any(text in out for text in absent)


def test_plan_is_printed_before_the_first_write(capsys):
    fake = FakeGrist()
    seen = []

    def hook(request):
        if request.method != "GET" and not seen:
            seen.append(capsys.readouterr().out)

    intercept(fake, hook)
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    before = seen[0]
    assert "--- Plan ---" in before and "New document 'BestYet' in workspace 'Work'" in before
    assert all(t.id in before for t in schema.TABLES)
    assert "Table1" not in before and "Created document" not in before


def test_calculated_columns_are_left_out_of_the_plan_but_named_when_they_fail(capsys):
    fake = incomplete_fake(table="Results", column="Value")  # fixed up, never announced
    assert run(fake, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Update the existing tables" in out and "Value" not in out
    fake.fail_on = "POST /api/docs/DOC_ID_HERE/tables/Results/columns"
    assert run(fake) == 1
    assert (
        "--- What did not complete ---\n  Calculated columns\n    - Results: Value"
        in capsys.readouterr().err
    )


# Done and next steps.


def test_new_document_ends_with_done_and_the_next_steps(capsys):
    argv = (*CREATE, "--name", "My Doc", "--seed-events")
    assert run(FakeGrist(), *argv, env=ENV_NO_DOC) == 0
    captured = capsys.readouterr()
    out = captured.out
    assert captured.err == "" and KEY not in out
    done = out[out.index("Done. Created") :]
    assert (
        "Done. Created 'My Doc' in workspace 'Work' with 5 tables, the 'Personal Bests' page"
        in done
    )
    assert "and pre-loaded data." in done and "Needs attention" not in done
    assert "\n     GRIST_DOC_ID=NEWDOC\n" in done  # on a line of its own, before the check step
    assert done.index("GRIST_DOC_ID=NEWDOC") < done.index("3. Then run bestyet-init --check")
    for part in ("Setting up", "Plan", "Next steps"):
        assert f"--- {part} ---" in out


def test_existing_document_says_what_was_added_and_needs_no_settings_step(capsys):
    assert run(incomplete_fake()) == 0
    out = capsys.readouterr().out
    assert "Done. Added the table Meets." in out
    assert "GRIST_DOC_ID" not in out and "\n  2. Then run bestyet-init --check" in out
    assert "--- Setting up ---" not in out  # nothing to show under it


def test_the_weights_hint_depends_on_whether_there_are_sample_implements(capsys):
    assert run(FakeGrist(), *CREATE, "--seed-events", env=ENV_NO_DOC) == 0
    assert HINT in capsys.readouterr().out
    for flag in ("--seed-implements", "--demo-data"):
        assert run(FakeGrist(), *CREATE, flag, env=ENV_NO_DOC) == 0
        assert HINT not in capsys.readouterr().out
    # In an existing document, the owner's own implements mean no hint.
    assert run(incomplete_fake()) == 0
    assert HINT in capsys.readouterr().out
    fake = incomplete_fake()
    fake.rows["Implements"] = [{"id": 1, "fields": {"Event": 1, "Grade": "Grade 8", "Weight": "x"}}]
    assert run(fake) == 0
    assert HINT not in capsys.readouterr().out


def test_events_left_out_say_where_to_add_them(capsys):
    assert run(FakeGrist(), *CREATE, env=ENV_NO_DOC) == 0
    out = capsys.readouterr().out.lower()
    assert "add your events by hand" in out and "cannot record results" in out


def test_default_table_warning_says_how_to_fix_it(capsys):
    fake = FakeGrist({"Table1": DEFAULTS})
    assert run(fake, "--allow-existing-tables") == 0
    out = capsys.readouterr().out
    assert "Table1" in fake.tables and out.count("Table1 not removed: the document was not") == 1
    line = next(x for x in out.splitlines() if "To fix:" in x)
    assert "To fix: remove Table1 in Grist" in line and "--allow-existing-tables" not in line
    assert "Needs attention" in out
    fake = FakeGrist(new_doc_tables={"Table1": DEFAULTS, "Notes": {}})
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 0
    assert "--allow-existing-tables on re-runs" in capsys.readouterr().out


def test_refusal_mentions_the_default_table_and_create_only_when_it_is_there(capsys):
    assert run(FakeGrist({"Table1": DEFAULTS})) == 1
    err = capsys.readouterr().err
    assert "empty Table1" in err and "--create" in err
    assert run(FakeGrist({"Notes": {}})) == 1
    assert "empty Table1" not in capsys.readouterr().err


# Failure reports.


def test_failure_in_an_existing_document_lists_what_was_done_and_what_was_not(capsys):
    fake = incomplete_fake()
    del fake.tables["Results"]["Mark"]
    fake.fail_on = "POST /api/docs/DOC_ID_HERE/tables/Results/columns"
    assert run(fake) == 1
    captured = capsys.readouterr()
    err = captured.err
    assert err.lstrip().startswith("--- Error ---\nGrist reported a server error (HTTP 500: boom)")
    assert "The run stopped part-way." in err
    assert "--- What was done ---\n  Tables\n    - Created Meets" in err
    failed = "Add missing columns to Results: Mark (failed; may be partly applied)"
    assert f"--- What did not complete ---\n  Tables\n    - {failed}" in err
    assert err.rstrip().endswith("it only adds what is missing.")
    assert "Summary" not in captured.out and KEY not in err + captured.out


def test_the_failed_step_is_marked_and_later_steps_are_not(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST /api/docs/DOC_ID_HERE/tables"
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert "What was done" not in err  # nothing to list
    assert (
        "Tables\n    - Create Athletes (failed; may be partly applied)\n    - Create Events" in err
    )
    assert "Personal bests\n    - Create a 'Personal Bests' page" in err  # not started: unmarked


def test_failure_after_create_names_the_document_and_gives_one_action_per_next_step(capsys):
    fake = FakeGrist()
    fake.fail_on = FAIL_TABLES
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    captured = capsys.readouterr()
    assert "Created the document 'BestYet' (id NEWDOC)." in captured.out  # shown straight away
    err = captured.err
    assert "--- What was done ---\n  Document\n    - Created document 'BestYet' (id NEWDOC)" in err
    assert "Removed Grist's empty default table (Table1)" in err
    assert "--- What did not complete ---" in err and KEY not in err
    steps = err.split("--- Next steps ---")[1]
    assert "1. Fix the cause shown under Error, if you can." in steps
    assert "2. Add this line to your settings (environment or .env): GRIST_DOC_ID=NEWDOC" in steps
    assert (
        "3. Then run bestyet-init again (without --create); it only adds what is missing." in steps
    )
    assert "Table1" not in steps  # it was removed: nothing to say about it


def test_failure_without_create_has_no_document_hint(capsys):
    fake = FakeGrist()
    fake.fail_on = "POST"
    assert run(fake) == 1
    err = capsys.readouterr().err
    assert "without --create" not in err and "GRIST_DOC_ID=" not in err
    assert "run bestyet-init again" in err


def test_failure_with_the_default_table_left_mentions_it_once(capsys):
    fake = FakeGrist()
    server_error(fake, lambda r: r.url.path.endswith("/NEWDOC/apply"))
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    err = capsys.readouterr().err
    assert "Table1" in fake.tables
    steps = err.split("--- Next steps ---")[1]
    assert "4. If Table1 is still in the document" in steps and steps.count("Table1") == 1
    assert "--- Error ---" in err and "--allow-existing-tables" in err


def _fail_page_rename(fake):
    server_error(fake, lambda r: b"_grist_Views" in r.content)  # the rename after the page


def _fail_trigger_update(fake):
    server_error(fake, lambda r: r.method == "PATCH" and b"recalcDeps" in r.content)


@pytest.mark.parametrize(
    ("break_it", "problems", "failed"),
    [
        (
            _fail_page_rename,
            ["was created but not named 'Personal Bests'", "view 1"],
            "Create a 'Personal Bests' page",
        ),
        (
            _fail_trigger_update,
            [
                "Created without its trigger dependency",
                "Results.Grade, Results.Division, Results.Implement",
            ],
            "Calculated columns",
        ),
    ],
)
def test_a_failure_a_rerun_cannot_fix_keeps_the_id_and_does_not_say_rerun(
    capsys, break_it, problems, failed
):
    fake = FakeGrist()
    break_it(fake)
    assert run(fake, *CREATE, env=ENV_NO_DOC) == 1
    err = capsys.readouterr().err
    assert all(p in err for p in problems) and failed in err
    assert "(failed; may be partly applied)" in err
    assert "id NEWDOC" in err and "GRIST_DOC_ID=NEWDOC" in err
    assert "Fix the problem described under Error in Grist" in err
    assert "To continue, re-run" not in err and "it only adds what is missing" not in err
    assert "Do not run --create again" in err and KEY not in err


def test_warnings_are_shown_when_the_run_stops_part_way(capsys):
    fake = FakeGrist({"Table1": DEFAULTS})
    fake.fail_on = "POST /api/docs/DOC_ID_HERE/tables"
    assert run(fake, "--allow-existing-tables") == 1
    err = capsys.readouterr().err
    assert "--- Next steps ---" in err and "Table1 not removed" in err and "To fix:" in err


def test_creating_while_grist_doc_id_is_set_warns_about_the_old_document(capsys):
    assert run(FakeGrist(), *CREATE, env=ENV) == 0
    assert "currently points at a different document" in capsys.readouterr().out
    fake = FakeGrist()
    fake.fail_on = FAIL_TABLES
    assert run(fake, *CREATE, env=ENV) == 1
    err = capsys.readouterr().err
    assert "currently points at a different document: GRIST_DOC_ID=NEWDOC" in err
