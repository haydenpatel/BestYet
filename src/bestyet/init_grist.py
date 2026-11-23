"""Init script: set up a Grist document so the schema contract is satisfied.

Run as ``python -m bestyet.init_grist`` (or ``bestyet-init``). It diffs ``schema.TABLES``
against the document and applies only additions through the REST API: missing tables, columns,
the PB summary table with its page, and ``visibleCol`` on Ref columns where it is unset. It
never drops, renames, retypes or overwrites; the one exception is Grist's own empty default
table in a document this run created (``remove_default_table``). The plan is printed first and
applied after confirmation (``--yes`` skips the prompt; without a terminal ``--yes`` or
``--dry-run`` is required). ``--check`` instead reads the document (GET only), reports every
difference from the contract (``bestyet.check``) and exits non-zero on any.

Seed rows are opt-in plan steps: ``--seed-events``, ``--seed-implements`` (which adds only the
events its rows use) and ``--demo-data`` (the last step; refused unless Athletes, Meets and
Results are all empty). Existing rows are never changed or duplicated: Events are matched by
Name (ignoring case and surrounding spaces) and Implements by that Event plus Grade and
Division exactly.

Output is for a person: plain-language lines on stdout, errors on stderr (``explain`` names the
host only, never credentials, path or key). A run that stopped part-way reports what was done
and what was not.
"""

import argparse
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace

import httpx

from bestyet import schema
from bestyet.check import check_document, format_differences, has_page, page_names
from bestyet.config import Config, ConfigError
from bestyet.grist import GristClient, GristError
from bestyet.init_errors import ErrorReport, InitError, Interrupted, _explaining, explain
from bestyet.init_steps import (
    DEFAULT_TABLE,
    AddColumns,
    CreateSummaryPage,
    CreateTable,
    LoadDemoData,
    SeedEvents,
    SeedImplements,
    SetVisibleCols,
    Step,
    _event_names,
    _missing_events,
    _missing_implements,
    _non_empty,
    _norm,
    _require_empty,
    column_payload,  # noqa: F401  (the tests import it from here)
    contract_table_ids,
    remove_default_table,
    unrelated_tables,
)


class PartialFailure(InitError):
    """The run stopped part-way: what was done, what failed and what was not done."""

    def __init__(
        self,
        done: Sequence[str],
        applied: Sequence["Step"],
        pending: Sequence["Step"],
        error: Exception,
        attention: Sequence[str] = (),
    ):
        super().__init__(str(error))
        self.done, self.applied, self.pending = list(done), list(applied), list(pending)
        self.error = error
        self.attention = list(attention)

    @property
    def rerun_safe(self) -> bool:
        """A plain re-run (which only adds) fixes it: the failure was a Grist call."""
        return isinstance(self.error, GristError)

    def _incomplete(self) -> list[tuple[str, list[str]]]:
        """What did not complete, in order, the failed step marked."""
        merged: dict[str, list[str]] = {}
        for heading, lines in grouped(self.pending[:1], hidden=True):
            merged[heading] = [f"{line} (failed; may be partly applied)" for line in lines]
        for heading, lines in grouped(self.pending[1:], hidden=True):
            merged.setdefault(heading, []).extend(lines)
        return list(merged.items())

    def report(self, config: Config, steps: Sequence[str]) -> str:
        reason = explain(self.error, config, "document") if self.rerun_safe else str(self.error)
        done = ([("Document", self.done)] if self.done else []) + grouped(
            self.applied, past=True, hidden=True
        )
        return error_report(
            reason,
            done=done,
            incomplete=self._incomplete(),
            steps=[*steps, *self.attention],
        )


def error_report(
    reason: str,
    done: Sequence[tuple[str, Sequence[str]]] = (),
    incomplete: Sequence[tuple[str, Sequence[str]]] = (),
    steps: Sequence[str] = (),
) -> str:
    """Labelled blocks, blank line between, empty blocks left out."""
    blocks = [["--- Error ---", reason, "The run stopped part-way."]]
    for title, groups in (
        ("What was done", done),
        ("What did not complete", incomplete),
    ):
        if groups:
            blocks.append([f"--- {title} ---", *group_lines(groups)])
    if steps:
        blocks.append(["--- Next steps ---", *(f"  {i}. {t}" for i, t in enumerate(steps, 1))])
    return "\n\n".join("\n".join(b) for b in blocks)


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
        return tuple(n for n, _ in schema.EXAMPLE_EVENTS)
    wanted: set[str] = set()
    if seed_implements or demo_data:
        wanted |= {_norm(n) for n in implement_event_names()}
    if demo_data:
        wanted |= {_norm(r[1]) for r in schema.DEMO_RESULTS}
    return tuple(n for n, _ in schema.EXAMPLE_EVENTS if _norm(n) in wanted)


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
                tuple(n for n, _ in missing_events),
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


# Plan and summary text: what the user cares about, grouped, not one line per internal step.

GROUPS = ("Tables", "Calculated columns", "Personal bests", "Display", "Pre-loaded data", "Other")


def grouped(
    steps: Sequence[Step], past: bool = False, hidden: bool = False
) -> list[tuple[str, list[str]]]:
    """Group steps by outcome, in plain words (`past`: what was done). The behind-the-scenes
    steps (calculated columns, names in Ref columns) show only when `hidden`: a failure report
    needs them, the plan does not."""
    out: dict[str, list[str]] = {g: [] for g in GROUPS}
    behind_the_scenes = False
    page_made = any(isinstance(s, CreateSummaryPage) for s in steps)
    new = [s.table.id for s in steps if isinstance(s, CreateTable)]
    if new:
        out["Tables"].append(f"{'Created' if past else 'Create'} {', '.join(new)}")
    example: list[str] = []
    for s in steps:
        if isinstance(s, AddColumns) and s.table == schema.SUMMARY.id:
            if not page_made:
                verb = "Added" if past else "Add"
                out["Personal bests"].append(
                    f"{verb} the missing column to the summary table: {s._names()}"
                )
        elif isinstance(s, AddColumns) and s.columns[0].formula is None:
            verb = "Added columns to" if past else "Add missing columns to"
            out["Tables"].append(f"{verb} {s.table}: {s._names()}")
        elif isinstance(s, AddColumns) and not hidden:
            behind_the_scenes = True
        elif isinstance(s, AddColumns):
            worked = [c.id for c in s.columns if c.is_formula]
            filled = [c.id for c in s.columns if not c.is_formula]
            parts = []
            if worked:
                parts.append(f"{', '.join(worked)} (worked out by Grist)")
            if filled:
                parts.append(f"{', '.join(filled)} (filled in automatically)")
            out["Calculated columns"].append(f"{s.table}: {', '.join(parts)}")
        elif isinstance(s, CreateSummaryPage):
            verb = "Created" if past else "Create"
            out["Personal bests"].append(
                f"{verb} a {s.summary.page!r} page showing each athlete's best per event"
            )
        elif isinstance(s, SetVisibleCols) and not hidden:
            behind_the_scenes = True
        elif isinstance(s, SetVisibleCols) and not out["Display"]:
            out["Display"].append(
                "Set Ref columns to show names instead of ids"
                if past
                else "Show names instead of ids in the Ref columns"
            )
        elif isinstance(s, SeedEvents):
            example.append(_seed_events_text(s, past))
        elif isinstance(s, SeedImplements):
            example.append(_seed_text("sample implement", s.add, s.have, past))
        elif isinstance(s, LoadDemoData):
            example.append("Added demo data" if past else f"Add demo data: {s._counts()}")
    out["Pre-loaded data"].extend(example)
    if behind_the_scenes and not any(out.values()):
        out["Other"].append("Update the existing tables (calculated columns, names in Ref columns)")
    return [(g, lines) for g, lines in out.items() if lines]


def _seed_text(noun: str, add: int, have: int, past: bool) -> str:
    """ "Add the 18 default events", or "Add 5 default events (13 already there)"."""
    verb = "Added" if past else "Add"
    plural = noun if add == 1 else f"{noun}s"
    if have == 0 and add > 1:
        return f"{verb} the {add} {plural}"
    note = "" if past or have == 0 else f" ({have} already there)"
    return f"{verb} {add} {plural}{note}"


def _seed_events_text(step: "SeedEvents", past: bool) -> str:
    """The full list as `_seed_text`, otherwise the few events the other seeds need: "Add 2
    events required for the sample implements (Discus and Shot Put)"."""
    add = len(step.add_names)
    if step.names is None or len(step.names) == len(schema.EXAMPLE_EVENTS):
        return _seed_text("default event", add, step.have, past)
    first = implement_event_names()  # the Implements events first, as in the guided note
    names = sorted(step.add_names, key=lambda n: first.index(n) if n in first else len(first))
    verb = "Added" if past else "Add"
    noun = "event" if add == 1 else "events"
    reason = "the demo data and sample implements" if step.for_demo else "the sample implements"
    note = "" if past or step.have == 0 else f"; {step.have} already there"
    return f"{verb} {add} {noun} required for {reason} ({_and(names)}{note})"


def _the_tables(names: Sequence[str]) -> str:
    """ "the table A" or "the tables A, B and C"."""
    unique = list(dict.fromkeys(names))
    return f"the table {unique[0]}" if len(unique) == 1 else f"the tables {_and(unique)}"


def _and(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def done_message(applied: Sequence[Step], doc: tuple[str, str] | None) -> str:
    """One sentence for a run that went as planned. `doc`: (name, workspace) if created."""
    tables = [s.table.id for s in applied if isinstance(s, CreateTable)]
    pages = [s.summary.page for s in applied if isinstance(s, CreateSummaryPage)]
    columns = [
        s.table
        for s in applied
        if isinstance(s, AddColumns)
        and s.table != schema.SUMMARY.id
        and s.columns[0].formula is None
    ]
    example = any(isinstance(s, (SeedEvents, SeedImplements, LoadDemoData)) for s in applied)
    if doc:
        parts = [f"{len(tables)} table{'' if len(tables) == 1 else 's'}"] if tables else []
        parts += [f"the {p!r} page" for p in pages]
        parts += ["pre-loaded data"] if example else []
        with_ = f" with {_and(parts)}" if parts else ""
        return f"Done. Created {doc[0]!r} in {doc[1]}{with_}."
    parts = [_the_tables(tables)] if tables else []
    parts += [f"missing columns in {_the_tables(columns)}"] if columns else []
    parts += [f"the {p!r} page" for p in pages]
    parts += ["pre-loaded data"] if example else []
    if not parts:
        parts = ["the missing behind-the-scenes parts"]
    return f"Done. Added {_and(parts)}."


def group_lines(groups: Sequence[tuple[str, Sequence[str]]]) -> list[str]:
    lines = []
    for heading, items in groups:
        lines.append(f"  {heading}")
        lines += [f"    - {item}" for item in items]
    return lines


def _section(out, title: str) -> None:
    """A plain-text heading between the parts of a run."""
    print(f"\n--- {title} ---", file=out)


def _print_bullets(heading: str, items: Sequence[str], out) -> None:
    print(heading, file=out)
    for item in items:
        print(f"  - {item}", file=out)


def _print_groups(groups: Sequence[tuple[str, Sequence[str]]], out) -> None:
    for line in group_lines(groups):
        print(line, file=out)


# Duplicate names.


class NameTaken(InitError):
    """A document with that name already exists in the workspace; a second is never created."""

    def __init__(self, existing: str, label: str):
        super().__init__(
            f"A document named {existing!r} already exists in {label}. Pick another name with "
            "--name, or run without --create using GRIST_DOC_ID to set that one up."
        )


def existing_docs(client: GristClient, workspace_id: int) -> list[dict]:
    """The documents in one workspace, as Grist lists them (`id` and `name`)."""
    for org in client.orgs():
        for w in client.workspaces(org["id"]):
            if w["id"] == workspace_id and "docs" in w:
                return list(w["docs"])
    return list(client.workspace(workspace_id).get("docs", []))


def name_clash(
    client: GristClient, workspace_id: int, name: str, docs: Sequence[dict] | None = None
) -> dict | None:
    """The document in the workspace that has this name (ignoring case and surrounding
    spaces), if any. `docs`: a listing already read (None: read it now)."""
    wanted = name.strip().casefold()
    if docs is None:
        docs = existing_docs(client, workspace_id)
    return next((d for d in docs if d["name"].strip().casefold() == wanted), None)


def _workspace(admin: GristClient, args: argparse.Namespace) -> tuple[int, str]:
    """The workspace's (id, label), looked up once per run."""
    if args.ws_resolved is None:
        args.ws_resolved = resolve_workspace_label(admin, args.workspace, args.org)
    return args.ws_resolved


def _workspace_docs(admin: GristClient, args: argparse.Namespace) -> list[dict]:
    """The workspace's documents, listed once per run (a final re-check lists again)."""
    if args.ws_docs is None:
        args.ws_docs = existing_docs(admin, _workspace(admin, args)[0])
    return args.ws_docs


# Workspace lookup.


def _orgs(client: GristClient, org: str | None) -> list[dict]:
    """The organisations the key can see, narrowed to `org` (an id or name) when given."""
    orgs = client.orgs()
    if org is not None:
        orgs = [o for o in orgs if str(o["id"]) == org or o.get("name") == org]
        if not orgs:
            raise InitError(ORG_NOT_FOUND.format(org=org))
    return orgs


def resolve_workspace_label(
    client: GristClient, workspace: str, org: str | None = None
) -> tuple[int, str]:
    """Return (workspace id, a label for messages): its name when it can be found."""
    if workspace.isascii() and workspace.isdigit():
        try:  # best effort: only for the plan's wording
            for o in client.orgs():
                for w in client.workspaces(o["id"]):
                    if str(w["id"]) == workspace and w.get("name"):
                        return int(workspace), f"workspace {w['name']!r}"
        except GristError:
            pass
        return int(workspace), f"workspace {workspace}"
    orgs = _orgs(client, org)
    matches = [w for o in orgs for w in client.workspaces(o["id"]) if w.get("name") == workspace]
    if not matches:
        raise InitError(
            f"Workspace {workspace!r} not found. Check --workspace (an id or exact name, "
            "with --org if the name is used in several organisations) and that the API key's "
            "user can see it."
        )
    if len(matches) > 1:
        ids = ", ".join(str(w["id"]) for w in matches)
        raise InitError(
            f"Workspace name {workspace!r} is ambiguous (ids: {ids}); use the id or --org"
        )
    return int(matches[0]["id"]), f"workspace {workspace!r}"


# CLI.


def parse_args(argv: Sequence[str] | None, can_ask: bool = False) -> argparse.Namespace:
    """`can_ask`: in a terminal a missing --workspace is asked for, not an error."""
    p = argparse.ArgumentParser(
        prog="bestyet-init",
        description="Add the BestYet tables and columns to a Grist document. Only adds.",
    )
    p.add_argument("--create", action="store_true", help="create a new empty document first")
    p.add_argument("--workspace", help="workspace id or name (with --create)")
    p.add_argument("--org", help="organisation id or name, to narrow --workspace")
    p.add_argument("--name", help="name of the new document (default BestYet)")
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument(
        "--check",
        action="store_true",
        help="read-only: report every difference from the contract, exit non-zero on any",
    )
    p.add_argument(
        "--seed-events",
        action="store_true",
        help="also add the default Events (the standard track and field list; off by default)",
    )
    p.add_argument(
        "--seed-implements",
        action="store_true",
        help=(
            "also add sample implement weights, by grade and division, and the Discus and "
            "Shot Put events if missing; only adds"
        ),
    )
    p.add_argument(
        "--allow-existing-tables",
        action="store_true",
        help="proceed even if the document has tables that are not part of the contract",
    )
    p.add_argument(
        "-y", "--yes", action="store_true", help="apply without asking (for non-interactive use)"
    )
    p.add_argument(
        "--demo-data",
        action="store_true",
        help=(
            "also load fictional athletes, meets and results (and the events and "
            "sample implements it uses); "
            "refused unless Athletes, Meets and Results are all empty"
        ),
    )
    p.set_defaults(  # set by main, run and the questions, not by flags
        guided=False,
        doc_id=None,
        old_doc_id="",
        settings_id=None,
        events_hint=False,
        implements_hint=False,
        ws_resolved=None,
        ws_docs=None,
    )
    args = p.parse_args(argv)
    if args.check:
        used = _flags_given(args)
        if used:
            p.error(f"--check is read-only and cannot be combined with {', '.join(used)}")
    if not can_ask:  # in a terminal the questions fill in what is missing
        if args.create and not args.workspace:
            p.error("--create needs --workspace")
        if not args.create and (args.workspace or args.org):
            p.error("--workspace and --org only apply with --create")
    return args


def _flags_given(args: argparse.Namespace) -> list[str]:
    """The options given other than --check, as typed."""
    return [
        flag
        for flag, on in (
            ("--create", args.create),
            ("--workspace", args.workspace),
            ("--org", args.org),
            ("--name", args.name),
            ("--dry-run", args.dry_run),
            ("--seed-events", args.seed_events),
            ("--seed-implements", args.seed_implements),
            ("--demo-data", args.demo_data),
            ("--allow-existing-tables", args.allow_existing_tables),
            ("--yes", args.yes),
        )
        if on
    ]


def _tty(stream) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def wants_guided(args: argparse.Namespace, stdin, stdout) -> bool:
    """Guided mode: in a terminal with no option, or with a required value missing."""
    if not (_tty(stdin) and _tty(stdout)) or args.check:
        return False
    incomplete = (args.create and not args.workspace) or (
        (args.workspace or args.org) and not args.create
    )
    return not _flags_given(args) or bool(incomplete)


def next_step(allow_existing: bool) -> str:
    if allow_existing:
        return f"remove {DEFAULT_TABLE} in Grist"
    return f"remove {DEFAULT_TABLE} in Grist, or pass --allow-existing-tables on re-runs"


def rerun_hint(doc_id: str) -> str:
    return (
        f"The document was created (id {doc_id}). To continue, re-run without --create and "
        f"with GRIST_DOC_ID={doc_id} (add --allow-existing-tables if {DEFAULT_TABLE} is still "
        "there); it only adds what is missing."
    )


def _fix_step(rerun_safe: bool) -> str:
    return (
        "Fix the cause shown under Error, if you can."
        if rerun_safe
        else "Fix the problem described under Error in Grist."
    )


def _settings_line(doc_id: str, old_doc_id: str) -> str:
    return "{}: {}".format(*settings_step(doc_id, old_doc_id)[:2])


def next_steps_after_create(
    doc_id: str, rerun_safe: bool, old_doc_id: str = "", table1_left: bool = False
) -> list[str]:
    """What to do when a run stopped after the document was created."""
    steps = [
        _fix_step(rerun_safe),
        _settings_line(doc_id, old_doc_id),
        "Then run bestyet-init again (without --create)"
        + ("; it only adds what is missing." if rerun_safe else "."),
    ]
    if table1_left:
        steps.append(
            f"If {DEFAULT_TABLE} is still in the document, add --allow-existing-tables to that run."
        )
    if not rerun_safe:
        steps.append("Do not run --create again: it would make another document.")
    return steps


def not_created_warning() -> str:
    return f"{DEFAULT_TABLE} not removed: the document was not created by this run"


ABORT = "aborted; nothing was changed"


ORG_NOT_FOUND = (
    "Organisation {org!r} not found. Check --org (an id or exact name) and that the API "
    "key's user can see it."
)


def _ask(out, prompt: str, stdin) -> str:
    out.write(prompt)
    out.flush()
    try:
        line = stdin.readline()
    except KeyboardInterrupt:
        print(file=out)
        raise Interrupted(ABORT) from None
    if not line:  # end of input
        print(file=out)
        raise Interrupted(ABORT)
    return line.strip()


def _ask_yes_no(out, question: str, stdin, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = _ask(out, f"{question} {hint} ", stdin).lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.", file=out)


def _ask_choice(
    out, question: str, options: Sequence[str], stdin, create_option: bool = False
) -> int | None:
    """Numbered list; returns the 0-based index chosen (None for C with `create_option`)."""
    print(question, file=out)
    for i, option in enumerate(options, 1):
        print(f"  {i}. {option}", file=out)
    prompt = f"Enter a number (1-{len(options)})"
    prompt += ", or C to create a new document: " if create_option else ": "
    while True:
        answer = _ask(out, prompt, stdin)
        if create_option and answer.lower() in ("c", "create"):
            return None
        if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer) - 1
        print(
            f"Please enter a number from 1 to {len(options)}{', or C' if create_option else ''}.",
            file=out,
        )


def _choose_workspace(client: GristClient, org: str | None, out, stdin) -> str | None:
    """Offer the visible workspaces; returns the chosen id, or None when the only one is
    declined (a lone workspace is never taken silently)."""
    orgs = _orgs(client, org)
    several = len(orgs) > 1
    found = [(o, w) for o in orgs for w in client.workspaces(o["id"])]
    if not found:
        raise InitError(
            "The API key cannot see any workspace to put a document in. Check GRIST_API_KEY, "
            "or ask for access to a workspace in Grist."
        )
    if len(found) == 1:
        name = found[0][1].get("name", found[0][1]["id"])
        if _ask_yes_no(out, f"Use workspace {str(name)!r}?", stdin, default=True):
            return str(found[0][1]["id"])
        return None
    labels = []
    for o, w in found:
        label = str(w.get("name", w["id"]))
        labels.append(f"{label} ({o.get('name', o['id'])})" if several else label)
    question = "Which workspace do you want to use?"
    return str(found[_ask_choice(out, question, labels, stdin)][1]["id"])


def guide(args: argparse.Namespace, config: Config, client_for, out, stdin) -> bool:
    """Ask what to set up and fill in `args` (a given flag skips its question); returns False
    when the user stops. Nothing is changed here."""
    _section(out, "Questions")
    if not args.yes:
        print("Nothing is changed until you confirm the plan.\n", file=out)
    doc_id = None
    create = args.create
    if config.grist_doc_id and not create and not args.workspace and not args.org:
        name = None
        try:
            with client_for(config.grist_doc_id) as client:
                name = client.document().get("name", "")
        except GristError as e:  # a stale or unreadable id must not stop the questions
            print(
                f"GRIST_DOC_ID is set but that document cannot be read. "
                f"{explain(e, config, 'document')} Choose another document below.\n",
                file=out,
            )
        if name is not None:
            question = f"GRIST_DOC_ID is set to the document {name!r}. Set up that document?"
            if _ask_yes_no(out, question, stdin, default=True):
                doc_id = config.grist_doc_id
    if doc_id is None:
        with _explaining(config, "workspace"), client_for("") as admin:
            if not args.workspace:
                args.workspace = _choose_workspace(admin, args.org, out, stdin)
                if args.workspace is None:
                    print(
                        "There is no other workspace visible to this API key. Create one in "
                        "Grist, then run this again. Nothing was changed.",
                        file=out,
                    )
                    return False
            ws_id, label = _workspace(admin, args)
            if not create:  # which document: one that is already there, or a new one
                docs = _workspace_docs(admin, args)
                if not docs:
                    create = True
                else:
                    pick = _ask_choice(
                        out,
                        f"Do you want to use an existing document in {label}?",
                        [d["name"] for d in docs],
                        stdin,
                        create_option=True,
                    )
                    create = pick is None
                    if pick is not None:
                        doc_id = docs[pick]["id"]
            if create:
                while True:
                    given = bool(args.name)
                    name = args.name or _ask(out, "Name for the new document [BestYet]: ", stdin)
                    args.name = name or "BestYet"
                    clash = name_clash(admin, ws_id, args.name, _workspace_docs(admin, args))
                    if not clash:
                        break
                    question = (
                        f"A document named {clash['name']!r} already exists in this workspace. "
                        "Use it instead?"
                    )
                    if _ask_yes_no(out, question, stdin, default=True):
                        create, doc_id = False, clash["id"]  # set that one up (it only adds)
                        break
                    if given:
                        print("Nothing was changed.", file=out)
                        return False
                    args.name = None
    args.create = create
    demo_allowed = True
    present_events: set[str] = set()  # a new document has no Events; an existing one may
    if not create:
        args.doc_id = doc_id
        with _explaining(config, "document"), client_for(doc_id) as client:
            tables = client.tables()
            extra = unrelated_tables(tables)
            if extra and not args.allow_existing_tables:
                print(
                    f"This document has tables that are not part of BestYet ({', '.join(extra)}). "
                    "The script can still add its own tables next to them.",
                    file=out,
                )
                if not _ask_yes_no(out, "Continue and add to it anyway?", stdin):
                    print("Nothing was changed.", file=out)
                    return False
                args.allow_existing_tables = True
            demo_allowed = not _non_empty(client, contract_table_ids(tables))
            if any(t["id"] == "Events" for t in tables):
                present_events = {_norm(n) for n in _event_names(client.records("Events")).values()}
    if args.yes or args.dry_run:  # only a required value was missing: flag defaults apply
        return True
    if demo_allowed and not args.demo_data:
        args.demo_data = _ask_yes_no(
            out,
            "Add demo data (a few fictional athletes, meets and results, with the events and "
            "sample implements they use)?",
            stdin,
        )
    if not args.demo_data:  # demo data covers the Events and Implements it needs
        if not args.seed_events:
            args.seed_events = _ask_yes_no(
                out, "Add default events (standard track and field list)?", stdin
            )
        if not args.seed_implements:
            question = "Add sample implement weights (by grade and division)?"
            names = implement_event_names()
            if not args.seed_events and any(_norm(n) not in present_events for n in names):
                # Yes would add events that are not otherwise being added: say so, after the
                # question and before the answer.
                kind = "events" if len(names) > 1 else "event"
                print(question, file=out)
                question = (
                    f"This also adds the {_and(list(names))} {kind}, which the implements use."
                )
            args.seed_implements = _ask_yes_no(out, question, stdin)
    return True


def _settings_id(config: Config, args: argparse.Namespace) -> str | None:
    """The id for the user's settings, when the document is not the one GRIST_DOC_ID has."""
    return config.grist_doc_id if config.grist_doc_id != args.old_doc_id else None


def settings_step(doc_id: str, old_doc_id: str) -> tuple[str, str, str | None]:
    """The settings step: (instruction, the line for the settings, a closing note or None)."""
    assignment = f"GRIST_DOC_ID={doc_id}"
    if old_doc_id:
        update = (
            "Update GRIST_DOC_ID in your settings (environment or .env), which currently "
            "points at a different document"
        )
        note = "Until you do, bestyet-init and the app keep using that other document."
        return update, assignment, note
    return "Add this line to your settings (environment or .env)", assignment, None


EVENTS_HINT = (
    "Add your events by hand in the Events table in Grist (Name, and Kind: Time or "
    "Distance); without events you cannot record results."
)


IMPLEMENTS_HINT = (
    "Add implement weights by hand in the Implements table in Grist (Event, Grade, Division "
    "and Weight); until you do, the Implement column on results stays blank."
)


def print_next_steps(
    out,
    doc_id: str | None,
    old_doc_id: str,
    check: bool = True,
    events_hint: bool = False,
    implements_hint: bool = False,
) -> None:
    """The closing Next steps: settings, events hint, implements hint, then the --check
    pointer; numbered when there are several."""
    if not doc_id and not check and not events_hint and not implements_hint:
        return
    _section(out, "Next steps")
    numbered = bool(doc_id) + events_hint + implements_hint + check > 1
    n = 0
    if doc_id:
        instruction, assignment, note = settings_step(doc_id, old_doc_id)
        n += 1
        print(f"  {n}. {instruction}; this script does not change your settings:", file=out)
        print(f"     {assignment}", file=out)
        if note:
            print(f"     {note}", file=out)
    if events_hint:
        n += 1
        print(f"  {n}. {EVENTS_HINT}" if numbered else f"  - {EVENTS_HINT}", file=out)
    if implements_hint:
        n += 1
        print(f"  {n}. {IMPLEMENTS_HINT}" if numbered else f"  - {IMPLEMENTS_HINT}", file=out)
    if check:
        text = "run bestyet-init --check to confirm the document matches the contract."
        n += 1
        if numbered:
            print(f"  {n}. Then {text}", file=out)
        else:
            print(f"  - {text[0].upper()}{text[1:]}", file=out)


def run(
    args: argparse.Namespace,
    config: Config,
    out,
    transport: httpx.BaseTransport | None = None,
) -> None:
    def client_for(doc_id: str) -> GristClient:
        return GristClient(config.grist_url, doc_id, config.grist_api_key, transport=transport)

    args.old_doc_id = config.grist_doc_id  # what the settings point at before this run
    if args.guided:
        if not guide(args, config, client_for, out, sys.stdin):
            return
        if args.doc_id:  # an existing document picked from the workspace's list
            config = replace(config, grist_doc_id=args.doc_id)
    args.name = args.name or "BestYet"

    if args.check:
        with _explaining(config, "document"), client_for(config.grist_doc_id) as client:
            differences = check_document(client)
        if differences:
            print(format_differences(differences), file=out)
            print(
                "Running bestyet-init without --check adds what is missing; other differences "
                "(a changed type or name, say) need fixing in Grist.",
                file=out,
            )
            n = len(differences)
            raise InitError(
                f"the document has {n} difference{'' if n == 1 else 's'} from the contract"
            )
        print("The document matches the BestYet contract: no differences.", file=out)
        return

    if not args.create:
        if not config.grist_doc_id:
            raise InitError("GRIST_DOC_ID is missing (or use --create)")
        args.settings_id = _settings_id(config, args)
        with _explaining(config, "document"), client_for(config.grist_doc_id) as client:
            _set_up_existing(client, config, args, out)
        return

    with _explaining(config, "workspace"), client_for("") as admin:
        workspace_id, workspace_label = _workspace(admin, args)
    new_doc = (args.name, workspace_label)
    with _explaining(config, "workspace"), client_for("") as admin:
        clash = name_clash(admin, workspace_id, args.name, _workspace_docs(admin, args))
    if clash:
        taken = f"A document named {clash['name']!r} already exists in {workspace_label}."
        if args.yes or not _tty(sys.stdin):  # never switch to an existing document unseen
            raise NameTaken(clash["name"], workspace_label)
        if args.dry_run:
            print(
                f"{taken} A real run asks whether to use it instead; this is the plan for that.\n",
                file=out,
            )
        elif not _ask_yes_no(out, f"{taken} Use it instead?", sys.stdin, default=True):
            print("Nothing was changed.", file=out)
            return
        config = replace(config, grist_doc_id=clash["id"])
        args.settings_id = _settings_id(config, args)
        with _explaining(config, "document"), client_for(config.grist_doc_id) as client:
            _set_up_existing(client, config, args, out)
        return
    args.events_hint = not args.seed_events
    args.implements_hint = not (args.seed_implements or args.demo_data)
    # A new document is empty apart from that default table, so the plan is the full contract.
    plan = build_plan(
        {},
        seed_implements=args.seed_implements,
        demo_data=args.demo_data,
        seed_events=args.seed_events,
    )
    if not _show_plan(plan, new_doc, args, out) or args.dry_run or not _confirm(args, out):
        return
    with _explaining(config, "workspace"), client_for("") as admin:
        # Re-check right before creating: the workspace can change after the first look.
        if clash := name_clash(admin, workspace_id, args.name):
            raise NameTaken(clash["name"], workspace_label)
        doc_id = admin.create_doc(workspace_id, args.name)
    _section(out, "Setting up")
    print(f"Created the document {args.name!r} (id {doc_id}).", file=out)
    done = [f"Created document {args.name!r} (id {doc_id})"]
    try:
        with client_for(doc_id) as client:
            removed, warning = remove_default_table(client)
            if removed:
                done.append(f"Removed Grist's empty default table ({DEFAULT_TABLE})")
            _apply(
                plan,
                client,
                [warning] if warning else [],
                args,
                out,
                done,
                doc_id,
                doc=(args.name, workspace_label),
            )
    except PartialFailure as e:
        table1_left = not any(d.startswith("Removed") for d in e.done) and not e.attention
        steps = next_steps_after_create(doc_id, e.rerun_safe, args.old_doc_id, table1_left)
        raise ErrorReport(e.report(config, steps)) from None
    except (GristError, InitError) as e:
        safe = isinstance(e, GristError)
        reason = explain(e, config, "document") if safe else str(e)
        raise ErrorReport(
            error_report(
                reason,
                done=[("Document", done)],
                steps=next_steps_after_create(doc_id, safe, args.old_doc_id, True),
            )
        ) from None
    except KeyboardInterrupt:
        raise Interrupted(f"aborted. {rerun_hint(doc_id)}") from None


def _set_up_existing(client: GristClient, config: Config, args: argparse.Namespace, out) -> None:
    tables = client.tables()
    extra = unrelated_tables(tables)
    if extra and not args.allow_existing_tables:
        hint = ""
        if DEFAULT_TABLE in extra:
            hint = (
                f". A blank document made in Grist has an empty {DEFAULT_TABLE}; "
                "--create avoids this"
            )
        raise InitError(
            "The document has tables that are not part of BestYet ("
            + ", ".join(extra)
            + "); pass --allow-existing-tables to add to it anyway"
            + hint
        )
    warnings = [not_created_warning()] if DEFAULT_TABLE in extra else []
    present = contract_table_ids(tables)
    actual = {t: {c["id"]: c.get("fields", {}) for c in client.columns(t)} for t in present}
    summary_tables = [t["id"] for t in tables if t.get("fields", {}).get("summarySourceTable")]
    events = client.records("Events") if "Events" in present else None
    implements = client.records("Implements") if "Implements" in present else None
    args.events_hint = not args.seed_events and bool(_missing_events(events or []))
    args.implements_hint = not (args.seed_implements or args.demo_data) and not implements
    if args.demo_data:
        _require_empty(client, present, ". Nothing was changed")
    plan = build_plan(
        actual,
        page_names(client),
        summary_tables,
        events,
        implements,
        args.seed_implements,
        args.demo_data,
        args.seed_events,
    )
    if not _show_plan(plan, None, args, out) or args.dry_run or not _confirm(args, out):
        _finish(plan, warnings, args, out)
        if not plan.steps and not args.dry_run:  # the user's only chance to learn the id
            print_next_steps(out, args.settings_id, args.old_doc_id, check=False)
        return
    try:
        _apply(plan, client, warnings, args, out, doc_id=args.settings_id)
    except PartialFailure as e:
        steps = [_fix_step(e.rerun_safe)]
        if args.settings_id:
            steps.append(_settings_line(args.settings_id, args.old_doc_id))
        if e.rerun_safe:
            steps.append("Then run bestyet-init again; it only adds what is missing.")
        raise ErrorReport(e.report(config, steps)) from None
    except KeyboardInterrupt:
        raise Interrupted("aborted; re-run to add what is still missing") from None


def _new_document_groups(plan: Plan, new_doc: tuple[str, str]) -> list[tuple[str, list[str]]]:
    """A new document's tables and page share one heading; the rest follows as usual."""
    name, label = new_doc
    tables = [s.table.id for s in plan.steps if isinstance(s, CreateTable)]
    pages = [s.summary.page for s in plan.steps if isinstance(s, CreateSummaryPage)]
    lines = [f"Tables: {', '.join(tables)}"] if tables else []
    lines += [f"A {p!r} page showing each athlete's best per event" for p in pages]
    rest = [(h, ls) for h, ls in grouped(plan.steps) if h not in ("Tables", "Personal bests")]
    return [(f"New document {name!r} in {label}", lines), *rest]


def _show_plan(plan: Plan, new_doc: tuple[str, str] | None, args: argparse.Namespace, out) -> bool:
    """Print notes, conflicts and the whole plan. Returns False when there is nothing to do."""
    if plan.notes:
        _print_bullets(
            "Left as is (differs from the contract; this script never changes existing columns):",
            plan.notes,
            out,
        )
    if plan.conflicts:
        _print_bullets(
            "Needs a manual fix in Grist (this script changes nothing for these):",
            plan.conflicts,
            out,
        )
    if not new_doc and not plan.steps:
        print(
            ("\n" if args.guided else "")
            + "Nothing to do: the document already has everything this script adds. "
            "To verify it against the contract, run bestyet-init --check.",
            file=out,
        )
        return False
    _section(out, "Plan")
    if new_doc:
        _print_groups(_new_document_groups(plan, new_doc), out)
    else:
        _print_groups(grouped(plan.steps), out)
    if args.dry_run:
        print("Dry run: nothing was changed. Run again without --dry-run to apply.", file=out)
    return True


def _confirm(args: argparse.Namespace, out) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise InitError(
            "not interactive; pass --yes to apply, or --dry-run to only see the plan "
            "(in a terminal, run it without options to be asked questions)"
        )
    answer = _ask(out, "Apply these changes? [y/N] ", sys.stdin)  # EOF or Ctrl-C: aborted
    if answer.lower() in ("y", "yes"):
        return True
    print("Not applied: nothing was changed.", file=out)
    return False


def _apply(
    plan: Plan,
    client: GristClient,
    warnings: Sequence[str],
    args: argparse.Namespace,
    out,
    done: Sequence[str] = (),
    doc_id: str | None = None,
    doc: tuple[str, str] | None = None,
) -> None:
    """Apply the steps. `done`: what this run already did; `doc`: the document it created."""
    applied: list[Step] = []
    for i, step in enumerate(plan.steps):
        try:
            step.apply(client)
        except (GristError, InitError) as e:
            fix = next_step(args.allow_existing_tables)
            attention = [f"{w}. To fix: {fix}" for w in warnings]
            raise PartialFailure(done, applied, plan.steps[i:], e, attention) from None
        applied.append(step)
    _finish(plan, warnings, args, out, doc_id, applied, doc)


def _finish(
    plan: Plan,
    warnings: Sequence[str],
    args: argparse.Namespace,
    out,
    doc_id: str | None = None,
    applied: Sequence[Step] | None = None,
    doc: tuple[str, str] | None = None,
) -> None:
    """Print the closing message, warnings and next steps (`applied` None: nothing applied)."""
    if applied is not None:
        print(f"\n{done_message(applied, doc)}", file=out)
    if warnings:
        fix = next_step(args.allow_existing_tables)
        _print_bullets("Needs attention:", [f"{w}. To fix: {fix}" for w in warnings], out)
    if plan.conflicts:
        raise InitError(
            "The document differs from the contract in ways this script does not change "
            "(listed above). Fix them in Grist, then re-run."
        )
    if applied is not None:
        print_next_steps(
            out,
            doc_id,
            args.old_doc_id,
            events_hint=args.events_hint,
            implements_hint=args.implements_hint,
        )


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    can_ask = _tty(sys.stdin) and _tty(sys.stdout)
    args = parse_args(argv, can_ask)
    args.guided = wants_guided(args, sys.stdin, sys.stdout)
    config = None
    try:
        config = Config.from_env(environ, require_doc_id=not (args.create or args.guided))
        run(args, config, sys.stdout, transport)
    except Interrupted as e:
        print(f"error: {e}", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print("error: aborted", file=sys.stderr)
        return 130
    except ConfigError as e:
        print(f"error: {e}. See Configuration in the README.", file=sys.stderr)
        return 1
    except GristError as e:  # not caught and explained by run(), e.g. a failure while closing
        print(f"error: {explain(e, config)}", file=sys.stderr)
        return 1
    except ErrorReport as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    except InitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - no traceback (with request details) on the terminal
        print(f"error: unexpected failure ({type(e).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
