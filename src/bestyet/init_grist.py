"""Init script: set up a Grist document so the schema contract is satisfied.

Run as ``python -m bestyet.init_grist`` (or ``bestyet-init``). It builds a plan by diffing
``schema.TABLES`` against the document, then applies only additions through the REST API:
missing tables, columns (plain, formula and trigger), the PB summary table with its page, and
``visibleCol`` on Ref columns where it is unset. It never drops, renames, retypes or
overwrites; the one exception is Grist's own empty default table in a document this run
created (see ``remove_default_table``). The whole plan is printed first and applied only
after confirmation (``--yes`` skips the prompt; without a terminal ``--yes`` or ``--dry-run``
is required). ``--check`` instead reads the document (GET only), reports every difference from
the contract (``bestyet.check``, also run by the app at startup) and exits non-zero on any.

Seed rows are opt-in plan steps (a fresh document ends up with empty Events and Implements
tables unless asked): the default Events with ``--seed-events``, the sample Implements with
``--seed-implements`` (which also adds only the events their rows use). ``--demo-data`` adds
fictional athletes, meets and results as the last step; it plans the Implements and only the
events it needs, and refuses (writing nothing) unless Athletes, Meets and Results are all
empty.

The Events and Implements seeds only add rows that are missing (Events matched by Name,
Implements by Event, Grade and Division, ignoring case and surrounding spaces); an existing row is never
changed or duplicated.

The plan is a list of small steps, each with ``describe()`` (for the plan), ``created()`` (for
the closing summary) and ``apply(client)``, so later work adds step types and extends
``build_plan`` without touching the CLI.

Output is for a person: plain-language lines on stdout, errors on stderr (``explain`` turns each
Grist failure into a message with what was tried, the likely cause and what to check; it names
the host only, never credentials, path or key). A run that applied changes ends with a summary
and the next steps; a run that stopped part-way reports what was done and what was not.
"""

import argparse
import json
import re
import sys
from collections.abc import Collection, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

import httpx

from bestyet import dates, schema
from bestyet.check import check_document, format_differences, has_page, page_names
from bestyet.config import Config, ConfigError
from bestyet.grist import GristClient, GristError


class InitError(Exception):
    """A failure with a message that is safe to print as is."""


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
        """A plain re-run (which only adds) is the fix: the failure was a Grist call."""
        return isinstance(self.error, GristError)

    def _incomplete(self) -> list[tuple[str, list[str]]]:
        """What did not complete, in order; the failed step is marked on its lines."""
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


class ErrorReport(InitError):
    """An error text with its own headings, printed as is (no "error:" prefix)."""


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


_CAUSES = {
    "dns": "the host name could not be resolved (DNS)",
    "refused": "the connection was refused (is the server running, and is the port right?)",
    "timeout": "the connection timed out",
    "tls": "a TLS or certificate problem",
    "other": "the connection failed",
}


def explain(e: GristError, config: Config, what: str = "") -> str:
    """A readable message for a failed Grist call. `what` is "workspace" or "document" when
    a 404 can be pinned on one. Names the host only: never credentials, path, query or key."""
    if e.status == 0:
        parts = urlsplit(config.grist_url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"  # IPv6
        if parts.port:
            host = f"{host}:{parts.port}"
        return (
            f"Could not reach Grist at {host}: {_CAUSES.get(e.cause or '', _CAUSES['other'])}. "
            "Check GRIST_URL, your network and that the Grist server is up."
        )
    if e.cause in ("redirect", "invalid_json"):
        return (
            "Grist gave an unexpected reply. Check that GRIST_URL is the address of a Grist "
            "server (its final http(s) address, without a path)."
        )
    if e.status == 401:
        return "Grist rejected the API key (HTTP 401). Check GRIST_API_KEY."
    if e.status == 403:
        target = f" the {what}" if what else " it"
        return (
            "Grist denied access (HTTP 403). Check that the API key's user can access"
            + target
            + "."
        )
    if e.status == 404 and what == "workspace":
        return (
            "Grist could not find that workspace (HTTP 404). Check --workspace and that the API "
            "key's user can see it."
        )
    document_request = e.method == "GET" and re.fullmatch(r"docs/[^/]+(/tables)?", e.path or "")
    if e.status == 404 and what == "document" and document_request:
        return (
            "Grist could not find the document (HTTP 404). Check GRIST_DOC_ID and that the API "
            "key's user can access it."
        )
    if e.status == 404:
        return (
            f"Grist could not find something the script asked for (HTTP 404: {e.message}). "
            "The document may have been changed during the run; run bestyet-init --check to see "
            "what it looks like now."
        )
    if e.status >= 500:
        return (
            f"Grist reported a server error (HTTP {e.status}: {e.message}). Try again; "
            "if it persists, check the Grist server."
        )
    return str(e)


@contextmanager
def _explaining(config: Config, what: str):
    """Turn a GristError in the block into an InitError with a readable message."""
    try:
        yield
    except GristError as e:
        raise InitError(explain(e, config, what)) from None


# REST payloads. Kept small and separate: column `fields`, Choice `widgetOptions` JSON,
# `Ref:Table` types and the table-create body were checked against a live Grist server.


def column_payload(column: schema.Column) -> dict:
    """The REST body for creating one column.

    `recalcDeps` is not part of it: Grist rejects it (and can corrupt the document) when sent
    with the new column, so it is set in a second call, see `AddColumns.apply`."""
    fields: dict = {"type": column.type}
    if column.label:
        fields["label"] = column.label
    if column.type == "Choice":
        choices = column.choices
        if choices is None:
            choices = schema.DEFAULT_CHOICES.get(column.id, ())
        fields["widgetOptions"] = json.dumps({"choices": list(choices)})
    if column.formula is not None:
        fields["isFormula"] = column.is_formula
        fields["formula"] = column.formula
        if column.recalc_when is not None:
            fields["recalcWhen"] = column.recalc_when
    return {"id": column.id, "fields": fields}


def table_payload(table: schema.Table, columns: Sequence[schema.Column]) -> dict:
    return {"id": table.id, "columns": [column_payload(c) for c in columns]}


def _refs(client: GristClient, table: str) -> dict[str, dict]:
    """Column id -> column record (with its `fields.colRef`) for one table."""
    return {c["id"]: c for c in client.columns(table)}


def _check_ids(sent: Sequence[str], got: Sequence[str]) -> None:
    """Grist renames an id that clashes (ignoring case), e.g. `Name2`; stop if it did."""
    if list(sent) != list(got):
        raise InitError(
            f"Grist assigned ids {', '.join(got)} instead of {', '.join(sent)}; "
            "the document needs a manual fix in Grist"
        )


# Plan steps.


def _table_name(table: str) -> str:
    """A table as a user knows it: the summary table's generated id is not shown."""
    return "the personal bests table" if table == schema.SUMMARY.id else table


class Step:
    def describe(self) -> str:
        """What the step will do, for the plan."""
        raise NotImplementedError

    def created(self) -> str:
        """What the step added, for the summary."""
        raise NotImplementedError

    def tables(self) -> tuple[str, ...]:
        """Ids of the existing tables this step changes."""
        return ()

    def apply(self, client: GristClient) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class CreateTable(Step):
    table: schema.Table
    columns: tuple[schema.Column, ...]

    def describe(self) -> str:
        return f"Create table {self.table.id}"

    def created(self) -> str:
        return f"Created table {self.table.id}"

    def tables(self) -> tuple[str, ...]:
        return (self.table.id,)

    def apply(self, client: GristClient) -> None:
        sent = [self.table.id]
        _check_ids(sent, client.add_tables([table_payload(self.table, self.columns)]))


@dataclass(frozen=True)
class AddColumns(Step):
    table: str
    columns: tuple[schema.Column, ...]

    def describe(self) -> str:
        return f"Add missing columns to {_table_name(self.table)}: {self._names()}"

    def created(self) -> str:
        return f"Added columns to {_table_name(self.table)}: {self._names()}"

    def tables(self) -> tuple[str, ...]:
        return (self.table,)

    def _names(self) -> str:
        return ", ".join(c.id for c in self.columns)

    def apply(self, client: GristClient) -> None:
        sent = [c.id for c in self.columns]
        triggers = [c for c in self.columns if c.recalc_deps]
        _check_ids(sent, client.add_columns(self.table, [column_payload(c) for c in self.columns]))
        if triggers:
            # After the add: a trigger can depend on a column created in the same step.
            refs = {i: c["fields"]["colRef"] for i, c in _refs(client, self.table).items()}
            # A reference list is sent as ["L", ref, ...]; the column API wants it in an update.
            updates = [
                {"id": c.id, "fields": {"recalcDeps": ["L", *(refs[d] for d in c.recalc_deps)]}}
                for c in triggers
            ]
            try:
                client.update_columns(self.table, updates)
            except GristError as e:
                names = ", ".join(f"{self.table}.{c.id}" for c in triggers)
                raise InitError(
                    f"{e}. Created without its trigger dependency: {names}. A re-run leaves "
                    "existing columns alone: set the trigger columns in Grist, or remove them "
                    "and re-run"
                ) from None


@dataclass(frozen=True)
class SetVisibleCols(Step):
    """Show names instead of row ids in Ref columns: set `visibleCol` where it is unset."""

    table: str
    items: tuple[tuple[str, str, str], ...]  # (column id, target table id, target column id)

    def describe(self) -> str:
        return f"Show names instead of ids in {_table_name(self.table)}: {self._names()}"

    def created(self) -> str:
        return f"Showing names instead of ids in {_table_name(self.table)}: {self._names()}"

    def tables(self) -> tuple[str, ...]:
        return (self.table,)

    def _names(self) -> str:
        return ", ".join(i[0] for i in self.items)

    def apply(self, client: GristClient) -> None:
        have = _refs(client, self.table)
        targets: dict[str, dict[str, dict]] = {}
        updates = []
        for column, target_table, target_column in self.items:
            if have[column]["fields"].get("visibleCol"):  # re-checked: never overwrite a choice
                continue
            cols = targets.setdefault(target_table, _refs(client, target_table))
            ref = cols[target_column]["fields"]["colRef"]
            updates.append({"id": column, "fields": {"visibleCol": ref}})
        if updates:
            client.update_columns(self.table, updates)
            # visibleCol alone does not make Grist show the names; it needs a display column.
            client.apply(
                [
                    [
                        "SetDisplayFormula",
                        self.table,
                        None,
                        have[c]["fields"]["colRef"],
                        f"${c}.{t}",
                    ]
                    for c, _, t in self.items
                    if any(u["id"] == c for u in updates)
                ]
            )


@dataclass(frozen=True)
class CreateSummaryPage(Step):
    """Create the summary table through a view section on a new page.

    Grist keeps a summary table only while a view section uses it. Grist reuses the table
    if it already exists. Only runs when the page is missing; an existing page is never changed.
    """

    summary: schema.Summary

    def describe(self) -> str:
        s = self.summary
        return f"Create the {s.page!r} page with its summary table"

    def created(self) -> str:
        return f"Created the {self.summary.page!r} page with its summary table"

    def tables(self) -> tuple[str, ...]:
        return (self.summary.id,)

    def apply(self, client: GristClient) -> None:
        s = self.summary
        refs = _refs(client, s.source_table)
        table_ref = next(
            t["fields"]["tableRef"] for t in client.tables() if t["id"] == s.source_table
        )
        group = [refs[g]["fields"]["colRef"] for g in s.group_by]
        result = client.apply([["CreateViewSection", table_ref, 0, "record", group, None]])
        view_ref = result["retValues"][0]["viewRef"]
        try:
            client.apply([["UpdateRecord", "_grist_Views", view_ref, {"name": s.page}]])
        except GristError as e:
            raise InitError(
                f"{e}. A new page (view {view_ref}) was created but not named {s.page!r}; "
                "rename it in Grist, or remove it, before re-running (a re-run would add "
                "another page)"
            ) from None
        if not any(t["id"] == s.id for t in client.tables()):
            raise InitError(f"Grist did not create {s.id}; the document needs a manual fix")


def _norm(value: object) -> str:
    """Key for matching hand-edited values: ignores case and surrounding spaces."""
    return str(value or "").strip().casefold()


def _event_names(events: Sequence[Mapping]) -> dict[int, str]:
    """Events row id -> Name."""
    return {r["id"]: r["fields"].get("Name") for r in events}


def _missing_events(events: Sequence[Mapping]) -> tuple[tuple[str, str], ...]:
    have = {_norm(n) for n in _event_names(events).values()}
    return tuple(e for e in schema.EXAMPLE_EVENTS if _norm(e[0]) not in have)


def _missing_implements(
    events: Sequence[Mapping], implements: Sequence[Mapping]
) -> tuple[tuple[str, str, str, str], ...]:
    names = _event_names(events)
    have = {
        (
            _norm(names.get(r["fields"].get("Event"))),
            _norm(r["fields"].get("Grade")),
            _norm(r["fields"].get("Division")),
        )
        for r in implements
    }
    return tuple(i for i in schema.SAMPLE_IMPLEMENTS if tuple(_norm(v) for v in i[:3]) not in have)


@dataclass(frozen=True)
class SeedEvents(Step):
    """Add the default Events (from `names`, or all when None) whose Name is not there yet.
    `add_names` and `have`: what the plan found missing and how many were already there (for
    its wording only)."""

    names: tuple[str, ...] | None = None
    add_names: tuple[str, ...] = ()
    have: int = 0
    for_demo: bool = False  # the subset is also needed by the demo data (wording only)

    def describe(self) -> str:
        return "Add the default Events that are missing"

    def created(self) -> str:
        return "Added the default Events"

    def tables(self) -> tuple[str, ...]:
        return ("Events",)

    def apply(self, client: GristClient) -> None:
        # Re-read: the plan was shown before confirmation, and Grist can be edited meanwhile.
        wanted = None if self.names is None else {_norm(n) for n in self.names}
        rows = [
            e
            for e in _missing_events(client.records("Events"))
            if wanted is None or _norm(e[0]) in wanted
        ]
        if rows:
            client.add("Events", [{"Name": n, "Kind": k} for n, k in rows])


@dataclass(frozen=True)
class SeedImplements(Step):
    """Add the sample Implements whose Event, Grade and Division is not there yet. `add` and
    `have`: how many the plan found missing and already there (for its wording only)."""

    add: int = 0
    have: int = 0

    def describe(self) -> str:
        return "Add the sample Implements that are missing"

    def created(self) -> str:
        return "Added the sample Implements"

    def tables(self) -> tuple[str, ...]:
        return ("Implements",)

    def apply(self, client: GristClient) -> None:
        events = client.records("Events")
        rows = _missing_implements(events, client.records("Implements"))
        ids = {}
        for i, name in _event_names(events).items():
            ids.setdefault(_norm(name), i)
        add = []
        for event, grade, division, weight in rows:
            if _norm(event) not in ids:
                raise InitError(f"Events has no row named {event!r}; cannot add its Implements")
            add.append(
                {"Event": ids[_norm(event)], "Grade": grade, "Division": division, "Weight": weight}
            )
        if add:
            client.add("Implements", add)


DEMO_TABLES = ("Athletes", "Meets", "Results")


def _non_empty(client: GristClient, tables: Collection[str]) -> list[str]:
    """The demo target tables (among `tables`) that already have rows."""
    return [t for t in DEMO_TABLES if t in tables and client.records(t)]


def _require_empty(client: GristClient, tables: Collection[str], note: str = "") -> None:
    if full := _non_empty(client, tables):
        raise InitError(
            f"--demo-data only loads into empty Athletes, Meets and Results; {', '.join(full)} "
            f"already {'has' if len(full) == 1 else 'have'} rows{note}"
        )


@dataclass(frozen=True)
class LoadDemoData(Step):
    """Add the fictional athletes, meets and results.

    Results go in with Athlete and Event in one call, so Grade, Division and Implement resolve from
    their trigger formulas. Needs the Events (and Implements) it uses from the steps before.
    """

    def describe(self) -> str:
        return f"Add demo data ({self._counts()}; only if those tables are empty)"

    def _counts(self) -> str:
        return (
            f"{len(schema.DEMO_ATHLETES)} athletes, {len(schema.DEMO_MEETS)} meets, "
            f"{len(schema.DEMO_RESULTS)} results"
        )

    def created(self) -> str:
        return "Added demo data"

    def tables(self) -> tuple[str, ...]:
        return DEMO_TABLES

    def apply(self, client: GristClient) -> None:
        _require_empty(client, DEMO_TABLES)  # re-read: the document can change after the plan
        events: dict[str, int] = {}
        for i, name in _event_names(client.records("Events")).items():
            events.setdefault(_norm(name), i)
        wanted = {_norm(r[1]) for r in schema.DEMO_RESULTS}
        if missing := sorted(wanted - events.keys()):
            raise InitError(f"Events has no row named {', '.join(missing)}; cannot add demo data")
        athletes = dict(
            zip(
                (a[0] for a in schema.DEMO_ATHLETES),
                client.add(
                    "Athletes",
                    [{"Name": n, "Grade": g, "Division": d} for n, g, d in schema.DEMO_ATHLETES],
                ),
                strict=True,
            )
        )
        meets = dict(
            zip(
                (m[0] for m in schema.DEMO_MEETS),
                client.add(
                    "Meets",
                    [
                        {"Name": n, "Date": dates.to_grist(dates.parse_iso(d)), "Location": loc}
                        for n, d, loc in schema.DEMO_MEETS
                    ],
                ),
                strict=True,
            )
        )
        client.add(
            "Results",
            [
                {
                    "Athlete": athletes[a],
                    "Event": events[_norm(e)],
                    "Meet": meets[m],
                    "Mark": mark,
                    "Place": place,
                }
                for a, e, m, mark, place in schema.DEMO_RESULTS
            ],
        )


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    present: list[str] = field(default_factory=list)  # contract tables in the document at the start
    notes: list[str] = field(default_factory=list)  # reported, never acted on
    conflicts: list[str] = field(default_factory=list)  # case-only id clashes: the run fails

    def unchanged(self) -> list[str]:
        """Present tables that no step touches: they existed and needed nothing."""
        touched = {t for s in self.steps for t in s.tables()}
        return [t for t in self.present if t not in touched]


def _is_plain(column: schema.Column) -> bool:
    return column.formula is None


def _unset(fields: Mapping) -> bool:
    return not fields.get("visibleCol")


def unrelated_tables(actual_tables: Sequence[Mapping]) -> list[str]:
    """Ids of tables that are neither in the contract nor summary tables (ids ignore case)."""
    known = {t.id.lower() for t in schema.TABLES} | {schema.SUMMARY.id.lower()}
    return [
        t["id"]
        for t in actual_tables
        if t["id"].lower() not in known
        and not t.get("fields", {}).get("summarySourceTable")
        and not t["id"].startswith("_grist_")
    ]


def contract_table_ids(actual_tables: Sequence[Mapping]) -> list[str]:
    """Ids of the contract tables and the PB summary table that are in the document."""
    known = {t.id.lower() for t in schema.TABLES} | {schema.SUMMARY.id.lower()}
    return [t["id"] for t in actual_tables if t["id"].lower() in known]


def _visible_items(
    table: schema.Table, have: Mapping[str, Mapping] | None
) -> tuple[tuple[str, str, str], ...]:
    """Ref columns of `table` that should show names: all, or those with `visibleCol` unset."""
    return tuple(
        (c.id, c.ref_table, c.visible_col)
        for c in table.columns
        if c.visible_col and c.ref_table and (have is None or _unset(have.get(c.id, {})))
    )


def _diff_columns(
    plan: Plan, table: schema.Table, actual_cols: Mapping[str, Mapping]
) -> list[schema.Column]:
    """Return the contract columns missing from `actual_cols`; record notes and conflicts."""
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
    """Diff the contract against `actual`; additions only.

    `actual` maps table id -> {column id: column fields} for the contract tables and the
    summary table that are in the document. `pages` are the page names that exist and
    `summary_tables` the ids of real summary tables. `events` and `implements` are the
    existing rows of those tables (None: the table does not exist yet). Seeds are opt-in and
    each adds only what it needs: `seed_events` the whole default event list,
    `seed_implements` the missing sample Implements and only the events their rows use,
    `demo_data` the demo rows and only the events they and the Implements use. `demo_data`
    adds the demo step last; the caller checks that the tables are empty.

    Grist keeps ids unique ignoring case, so ids are matched ignoring case. A case-only
    difference is never added or renamed: it is a conflict the owner must fix in Grist.
    """
    plan = Plan(present=sorted(actual))
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
    """The default events a run adds: all of them for `--seed-events`, otherwise only the ones
    the Implements rows and the demo results use."""
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
    """Seeds are opt-in and add only the default Events `needed` (and missing)."""
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
    """Group steps by outcome, in plain words. `past`: what was done, not what will be.

    The behind-the-scenes steps (calculated columns, names shown in Ref columns) are left
    out unless `hidden`: the plan does not mention them, but a failure report must."""
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
    """ "Add the 18 default events" / "Add 5 default events (13 already there)" when the full
    list is in play, otherwise the few events the other seeds need: "Add 2 events required
    for the sample implements (Discus and Shot Put)"."""
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
    """One plain sentence for a run that went as planned. `doc`: (name, workspace label) of
    the document this run created."""
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
    """A heading that separates the parts of a run (plain text, readable in a log)."""
    print(f"\n--- {title} ---", file=out)


def _print_groups(groups: Sequence[tuple[str, Sequence[str]]], out) -> None:
    for line in group_lines(groups):
        print(line, file=out)


# Duplicate names.


class NameTaken(InitError):
    """A document with that name already exists in the workspace. A second one is never
    created; an interactive yes sets the existing one up instead."""

    def __init__(self, existing: str, label: str):
        super().__init__(
            f"A document named {existing!r} already exists in {label}. Pick another name with "
            "--name, or run without --create using GRIST_DOC_ID to set that one up."
        )
        self.existing, self.label = existing, label


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


def resolve_workspace(client: GristClient, workspace: str, org: str | None = None) -> int:
    """Return a workspace id from a numeric id or a name (optionally within one org)."""
    return resolve_workspace_label(client, workspace, org)[0]


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
    orgs = client.orgs()
    if org is not None:
        orgs = [o for o in orgs if str(o["id"]) == org or o.get("name") == org]
        if not orgs:
            raise InitError(ORG_NOT_FOUND.format(org=org))
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
    """`can_ask`: in a terminal a missing --workspace is asked for, so it is not an error."""
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
        used = [
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
        if used:
            p.error(f"--check is read-only and cannot be combined with {', '.join(used)}")
    if not can_ask:  # in a terminal the questions fill in what is missing
        if args.create and not args.workspace:
            p.error("--create needs --workspace")
        if not args.create and (args.workspace or args.org):
            p.error("--workspace and --org only apply with --create")
    return args


def _tty(stream) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def wants_guided(args: argparse.Namespace, stdin, stdout) -> bool:
    """Guided mode: in a terminal, with no option at all, or with a required value missing
    (`--create` without `--workspace`, `--workspace` or `--org` without `--create`)."""
    if not (_tty(stdin) and _tty(stdout)) or args.check:
        return False
    others = (
        args.create, args.workspace, args.org, args.name, args.dry_run,
        args.seed_events, args.seed_implements, args.allow_existing_tables, args.yes,
        args.demo_data,
    )  # fmt: skip
    incomplete = (args.create and not args.workspace) or (
        (args.workspace or args.org) and not args.create
    )
    return not any(others) or bool(incomplete)


DEFAULT_TABLE = "Table1"  # Grist puts an empty table with columns A, B, C in every new document
DEFAULT_COLUMNS = frozenset({"A", "B", "C"})


class Interrupted(Exception):
    """The run was interrupted (Ctrl-C); the message is safe to print as is."""


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


def next_steps_after_create(
    doc_id: str, rerun_safe: bool, old_doc_id: str = "", table1_left: bool = False
) -> list[str]:
    """What to do when a run stopped after the document was created."""
    steps = [
        "Fix the cause shown under Error, if you can."
        if rerun_safe
        else "Fix the problem described under Error in Grist.",
        "{}: {}".format(*settings_step(doc_id, old_doc_id)[:2]),
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


def default_table_state(client: GristClient) -> tuple[bool, str | None]:
    """Return (present, problem): is Table1 there, and why it must not be removed (if so).

    It may only be removed when it is the only user table, has only Grist's default columns
    and has no rows.
    """
    tables = client.tables()
    user = [t["id"] for t in tables if t["id"] in unrelated_tables(tables)]
    if DEFAULT_TABLE not in user:
        return False, None
    if user != [DEFAULT_TABLE]:
        others = ", ".join(t for t in user if t != DEFAULT_TABLE)
        return True, f"the document has other tables ({others})"
    extra = sorted(c["id"] for c in client.columns(DEFAULT_TABLE) if c["id"] not in DEFAULT_COLUMNS)
    if extra:
        return True, f"it has columns other than A, B, C ({', '.join(extra)})"
    if client.records(DEFAULT_TABLE) != []:
        return True, "it has rows"
    return True, None


def remove_default_table(client: GristClient) -> tuple[bool, str | None]:
    """Remove Grist's untouched, empty default table. Returns (removed, warning).

    Only for a document this run created. The conditions are checked here and again right
    before the removal; nothing else is ever removed.
    """
    for _ in range(2):
        present, problem = default_table_state(client)
        if not present:
            return False, None
        if problem:
            return False, f"{DEFAULT_TABLE} not removed: {problem}"
    client.apply([["RemoveTable", DEFAULT_TABLE]])
    return True, None


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
    """Numbered list; returns the 0-based index of the chosen option. With `create_option`
    the answer C (create a new document) is accepted too and returned as None."""
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
    """Offer the workspaces the key can see; returns the chosen id as a string, or None when
    the only one visible is declined. A lone workspace is never taken silently."""
    orgs = client.orgs()
    if org is not None:
        orgs = [o for o in orgs if str(o["id"]) == org or o.get("name") == org]
        if not orgs:
            raise InitError(ORG_NOT_FOUND.format(org=org))
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
    """Ask what to set up and fill in `args`. Returns False when the user chose to stop.

    A flag that is already given skips its question. Nothing is changed here."""
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
    """The id to tell the user to put in their settings: set when the document being set up
    is not the one GRIST_DOC_ID already points at."""
    return config.grist_doc_id if config.grist_doc_id != args.old_doc_id else None


def settings_step(doc_id: str, old_doc_id: str) -> tuple[str, str, str | None]:
    """The settings step of the next steps, in one place: (instruction, the line to put in
    the settings, a closing note or None). Callers join them as they need."""
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
    """The closing Next steps: the settings step (when the document is not the one in
    GRIST_DOC_ID), the events hint (when the full default list was not added), the implements
    hint (when the sample implements were not added), then the --check pointer."""
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
    args.implements_hint = not (args.seed_implements or args.demo_data) and bool(
        _missing_implements(events or [], implements or [])
    )
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
        steps = ["Fix the cause shown under Error, if you can."]
        if not e.rerun_safe:
            steps = ["Fix the problem described under Error in Grist."]
        if args.settings_id:
            steps.append("{}: {}".format(*settings_step(args.settings_id, args.old_doc_id)[:2]))
        if e.rerun_safe:
            steps.append("Then run bestyet-init again; it only adds what is missing.")
        raise ErrorReport(e.report(config, steps)) from None
    except KeyboardInterrupt:
        raise Interrupted("aborted; re-run to add what is still missing") from None


def _new_document_groups(plan: Plan, new_doc: tuple[str, str]) -> list[tuple[str, list[str]]]:
    """The plan of a new document: its document, tables and page always come together, so
    they share one heading; the rest (pre-loaded data) follows as usual."""
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
        print(
            "Left as is (differs from the contract; this script never changes existing columns):",
            file=out,
        )
        for note in plan.notes:
            print(f"  - {note}", file=out)
    if plan.conflicts:
        print("Needs a manual fix in Grist (this script changes nothing for these):", file=out)
        for conflict in plan.conflicts:
            print(f"  - {conflict}", file=out)
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
    """Apply the steps. `done` holds what was already done in this run (the document);
    `doc` is the (name, workspace) of a document this run created."""
    applied: list[Step] = []
    for i, step in enumerate(plan.steps):
        try:
            step.apply(client)
        except (GristError, InitError) as e:
            attention = [f"{w}. To fix: {next_step(args.allow_existing_tables)}" for w in warnings]
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
    """Print the closing message (after a run that applied changes), warnings and next steps.

    `applied` is None when nothing was applied (dry run, declined, nothing to do)."""
    if applied is not None:
        print(f"\n{done_message(applied, doc)}", file=out)
    if warnings:
        print("Needs attention:", file=out)
        for w in warnings:
            print(f"  - {w}. To fix: {next_step(args.allow_existing_tables)}", file=out)
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
