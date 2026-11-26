"""The plan: what build_plan decides to do about a document, before anything is written."""

import pytest
from init_helpers import (
    N_EVENTS,
    N_IMPLEMENTS,
    full_fake,
    full_tables,
    implement_rows,
    rename_column,
    rename_table,
)

from bestyet import schema
from bestyet.init_plan import build_plan, implement_event_names
from bestyet.init_steps import (
    AddColumns,
    CreateSummaryPage,
    CreateTable,
    LoadDemoData,
    SeedEvents,
    SeedImplements,
    SetVisibleCols,
    column_payload,
)

ALL_SEEDS = {"seed_events": True, "seed_implements": True, "demo_data": True}
SEEDS = (SeedEvents, SeedImplements, LoadDemoData)


def existing(tables=None, **kwargs):
    """Plan for a document that already holds `tables` (default: all of the contract)."""
    return build_plan(
        full_tables() if tables is None else tables,
        pages=[schema.SUMMARY.page],
        summary_tables=[schema.SUMMARY.id],
        **kwargs,
    )


def seeds(plan):
    return [s for s in plan.steps if isinstance(s, SEEDS)]


def test_a_new_document_plans_plain_columns_first_then_formulas_then_names_and_page():
    steps = build_plan({}).steps
    tables = [s for s in steps if isinstance(s, CreateTable)]
    assert [s.table.id for s in tables] == [t.id for t in schema.TABLES]
    results = next(s for s in tables if s.table.id == "Results")
    assert {c.id for c in results.columns} == {"Athlete", "Event", "Meet", "Mark", "Place"}
    later = next(s for s in steps if isinstance(s, AddColumns) and s.table == "Results")
    assert [c.id for c in later.columns] == [
        "Grade",
        "Division",
        "Implement",
        "Value",
        "Date",
        "IsPB",
        "IsNewPB",
    ]
    kinds = [type(s) for s in steps]
    assert kinds.index(CreateTable) < kinds.index(AddColumns) < kinds.index(SetVisibleCols)
    assert CreateSummaryPage in kinds


def test_column_payload_carries_the_type():
    assert column_payload(schema.table("Meets").columns[1]) == {
        "id": "Date",
        "fields": {"type": "Date"},
    }


def test_a_complete_document_plans_nothing():
    plan = existing()
    assert plan.steps == [] and plan.conflicts == []


@pytest.mark.parametrize(
    ("break_it", "pages", "summary_tables"),
    [
        (rename_table, [schema.SUMMARY.page], [schema.SUMMARY.id]),  # case-only table clash
        (rename_column, [schema.SUMMARY.page], [schema.SUMMARY.id]),  # case-only column clash
        (lambda t: t.pop(schema.SUMMARY.id), [schema.SUMMARY.page], []),  # page, no summary table
        (lambda t: None, [schema.SUMMARY.page], []),  # a plain table with the summary's name
    ],
)
def test_conflicts_are_reported_and_nothing_is_seeded(break_it, pages, summary_tables):
    tables = full_tables()
    break_it(tables)
    plan = build_plan(tables, pages=pages, summary_tables=summary_tables, **ALL_SEEDS)
    assert plan.conflicts
    assert seeds(plan) == []


def test_seeds_are_planned_in_order_events_implements_demo_data():
    plan = build_plan({}, **ALL_SEEDS)
    assert [type(s) for s in seeds(plan)] == [SeedEvents, SeedImplements, LoadDemoData]
    assert plan.steps[-3:] == seeds(plan)  # after the contract steps


def test_no_flags_no_seeds():
    assert seeds(build_plan({})) == []
    assert seeds(existing(events=[], implements=[])) == []


def test_seed_implements_needs_only_the_events_the_implements_use():
    plan = build_plan({}, seed_implements=True)
    events, implements = seeds(plan)
    assert set(events.names) == set(implement_event_names())
    assert events.add_names == events.names and events.have == 0
    assert (implements.add, implements.have) == (N_IMPLEMENTS, 0)


def test_demo_data_needs_the_events_its_results_use_as_well():
    events = seeds(build_plan({}, demo_data=True))[0]
    assert set(events.names) == set(implement_event_names()) | {r[1] for r in schema.DEMO_RESULTS}
    assert events.for_demo


def test_seed_events_counts_what_is_missing_and_what_is_there():
    rows = full_fake().rows["Events"]
    [step] = seeds(existing(events=rows[:-5], implements=[], seed_events=True))
    assert len(step.add_names) == 5 and step.have == N_EVENTS - 5
    assert seeds(existing(events=rows, implements=[], seed_events=True)) == []  # all there


def test_seed_implements_counts_what_is_missing_and_what_is_there():
    fake = full_fake()
    implements = implement_rows(fake, schema.SAMPLE_IMPLEMENTS[:-1])
    [step] = seeds(
        existing(events=fake.rows["Events"], implements=implements, seed_implements=True)
    )
    assert (step.add, step.have) == (1, N_IMPLEMENTS - 1)


def test_a_missing_event_is_planned_for_the_implements_that_use_it():
    rows = [r for r in full_fake().rows["Events"] if r["fields"]["Name"] != "Discus"]
    events, _ = seeds(existing(events=rows, implements=[], seed_implements=True))
    assert events.add_names == ("Discus",)
    assert events.have == len(implement_event_names()) - 1


def test_no_implements_step_when_all_are_already_there():
    fake = full_fake()
    implements = implement_rows(fake, schema.SAMPLE_IMPLEMENTS)
    plan = existing(events=fake.rows["Events"], implements=implements, **ALL_SEEDS)
    assert [type(s) for s in seeds(plan)] == [LoadDemoData]
