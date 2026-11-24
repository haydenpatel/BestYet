"""Init script plan building: diff the contract against the document into steps.

No questions and no output: the plan is data that the output module prints and the CLI applies.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from bestyet import schema
from bestyet.check import has_page
from bestyet.init_steps import (
    AddColumns,
    CreateSummaryPage,
    CreateTable,
    LoadDemoData,
    SeedEvents,
    SeedImplements,
    SetVisibleCols,
    Step,
    _missing_events,
    _missing_implements,
    _norm,
)


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # reported, never acted on
    conflicts: list[str] = field(default_factory=list)  # case-only id clashes: the run fails


def _is_plain(column: schema.Column) -> bool:
    return column.formula is None


def _unset(fields: Mapping) -> bool:
    return not fields.get("visibleCol")


def _visible_items(
    table: schema.Table, have: Mapping[str, Mapping] | None
) -> tuple[tuple[str, str, str], ...]:
    """Ref columns of `table` that should show names (those with `visibleCol` unset)."""
    return tuple(
        (c.id, c.ref_table, c.visible_col)
        for c in table.columns
        if c.visible_col and c.ref_table and (have is None or _unset(have.get(c.id, {})))
    )


def _diff_columns(
    plan: Plan, table: schema.Table, actual_cols: Mapping[str, Mapping]
) -> list[schema.Column]:
    """The contract columns missing from `actual_cols`; notes and conflicts go on `plan`."""
    have = {c.lower(): c for c in actual_cols}
    missing = []
    for c in table.columns:
        found = have.get(c.id.lower())
        if found is None:
            missing.append(c)
        elif found != c.id:
            plan.conflicts.append(f"{table.id}: column {found!r} exists, contract says {c.id!r}")
        elif actual_cols[found].get("type") != c.type:
            plan.notes.append(
                f"{table.id}.{c.id} has type {actual_cols[found].get('type')}, "
                f"the contract says {c.type}"
            )
    return missing


def build_plan(
    actual: Mapping[str, Mapping[str, Mapping]],
    pages: Collection[str] = (),
    summary_tables: Collection[str] = (),
    events: Sequence[Mapping] | None = None,
    implements: Sequence[Mapping] | None = None,
    seed_implements: bool = False,
    demo_data: bool = False,
    seed_events: bool = False,
) -> Plan:
    """Diff the contract against `actual` (table id -> {column id: fields}); additions only.

    `pages` and `summary_tables` are the page names and real summary table ids; `events` and
    `implements` the existing rows (None: no such table yet). Each seed adds only what it
    needs; `demo_data` adds its step last and the caller checks the tables are empty. Ids are
    matched ignoring case: a case-only difference is a conflict for the owner to fix in Grist.
    """
    plan = Plan()
    tables = {t.lower(): t for t in actual}
    formula_steps: list[Step] = []
    visible_steps: list[Step] = []
    for table in schema.TABLES:
        actual_id = tables.get(table.id.lower())
        if actual_id is None:
            plan.steps.append(CreateTable(table, tuple(c for c in table.columns if _is_plain(c))))
            missing = [c for c in table.columns if not _is_plain(c)]
            have = None
        elif actual_id != table.id:
            plan.conflicts.append(f"table {actual_id!r} exists, contract says {table.id!r}")
            continue
        else:
            have = actual[actual_id]
            missing = _diff_columns(plan, table, have)
        plain = tuple(c for c in missing if _is_plain(c))
        if plain and actual_id is not None:
            plan.steps.append(AddColumns(table.id, plain))
        if formulas := tuple(c for c in missing if not _is_plain(c)):
            formula_steps.append(AddColumns(table.id, formulas))
        if items := _visible_items(table, have):
            visible_steps.append(SetVisibleCols(table.id, items))
    plan.steps.extend(formula_steps)
    plan.steps.extend(visible_steps)
    if not plan.conflicts:
        _plan_summary(plan, actual, pages, summary_tables)
    if not plan.conflicts:  # nothing is seeded into a document that needs a manual fix
        _plan_seeds(
            plan,
            events or [],
            implements or [],
            _needed_events(seed_events, seed_implements, demo_data),
            seed_implements or demo_data,
            demo_data,
        )
        if demo_data:
            plan.steps.append(LoadDemoData())
    return plan


def implement_event_names() -> tuple[str, ...]:
    """The events the sample Implements rows use, in order (from the schema)."""
    return tuple(dict.fromkeys(i[0] for i in schema.SAMPLE_IMPLEMENTS))


def _needed_events(seed_events: bool, seed_implements: bool, demo_data: bool) -> tuple[str, ...]:
    """The default events a run adds: all for `--seed-events`, else those the seeds use."""
    if seed_events:
        return tuple(n for n, *_ in schema.EXAMPLE_EVENTS)
    wanted: set[str] = set()
    if seed_implements or demo_data:
        wanted |= {_norm(n) for n in implement_event_names()}
    if demo_data:
        wanted |= {_norm(r[1]) for r in schema.DEMO_RESULTS}
    return tuple(n for n, *_ in schema.EXAMPLE_EVENTS if _norm(n) in wanted)


def _plan_seeds(
    plan: Plan,
    events: Sequence[Mapping],
    implements: Sequence[Mapping],
    needed: Sequence[str],
    implements_too: bool,
    demo: bool = False,
) -> None:
    """Add only the default Events `needed` that are missing, and the Implements if asked."""
    missing_events = tuple(e for e in _missing_events(events) if e[0] in needed)
    if missing_events:
        plan.steps.append(
            SeedEvents(
                tuple(needed),
                tuple(n for n, *_ in missing_events),
                len(needed) - len(missing_events),
                demo,
            )
        )
    # An Implements row of an Event that is not in the document yet counts as missing too.
    missing_implements = _missing_implements(events, implements)
    if implements_too and missing_implements:
        total = len(schema.SAMPLE_IMPLEMENTS)
        plan.steps.append(SeedImplements(len(missing_implements), total - len(missing_implements)))


def _plan_summary(
    plan: Plan,
    actual: Mapping[str, Mapping[str, Mapping]],
    pages: Collection[str],
    summary_tables: Collection[str],
) -> None:
    s = schema.SUMMARY
    exists = {t.lower(): t for t in actual}.get(s.id.lower())
    if exists is not None and (exists != s.id or exists not in summary_tables):
        plan.conflicts.append(f"table {exists!r} exists but is not the summary table {s.id!r}")
        return
    page_exists = has_page(pages, s.page)
    if page_exists and exists is None:
        plan.conflicts.append(
            f"page {s.page!r} exists but summary table {s.id!r} does not; "
            "add the summary table in Grist"
        )
        return
    if not page_exists:
        plan.steps.append(CreateSummaryPage(s))
    have = actual.get(s.id)
    missing = tuple(c for c in s.columns if have is None or c.id not in have)
    if missing:
        plan.steps.append(AddColumns(s.id, missing))
    source = schema.table(s.source_table)
    refs = {c.id: c for c in source.columns}
    items = tuple(
        (g, refs[g].ref_table, refs[g].visible_col)
        for g in s.group_by
        if refs[g].visible_col and (have is None or _unset(have.get(g, {})))
    )
    if items:
        plan.steps.append(SetVisibleCols(s.id, items))
