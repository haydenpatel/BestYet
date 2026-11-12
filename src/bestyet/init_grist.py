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

Seed rows are plan steps too: the example Events always, the example Implements only with
``--seed-implements``. ``--demo-data`` adds fictional athletes, meets and results as the last
step; it plans the example Events and Implements it needs, and refuses (writing nothing) unless
Athletes, Meets and Results are all empty.

The Events and Implements seeds only add rows that are missing (Events matched by Name,
Implements by Event and Grade, ignoring case and surrounding spaces); an existing row is never
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
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from bestyet import dates, schema
from bestyet.check import check_document, format_differences, page_names
from bestyet.config import Config, ConfigError
from bestyet.grist import GristClient, GristError


class InitError(Exception):
    """A failure with a message that is safe to print as is."""


class PartialFailure(InitError):
    """The run stopped part-way: what was done, what failed and what was not done."""

    def __init__(
        self,
        done: Sequence[str],
        pending: Sequence[str],
        error: Exception,
        attention: Sequence[str] = (),
    ):
        super().__init__(str(error))
        self.done, self.pending, self.error = list(done), list(pending), error
        self.attention = list(attention)

    @property
    def rerun_safe(self) -> bool:
        """A plain re-run (which only adds) is the fix: the failure was a Grist call."""
        return isinstance(self.error, GristError)

    def report(self, config: Config, fix: str) -> str:
        reason = explain(self.error, config, "document") if self.rerun_safe else str(self.error)
        lines = [reason, "The run stopped part-way."]
        lines.append("Done before the failure:" + ("" if self.done else " nothing"))
        lines += [f"  - {d}" for d in self.done]
        lines.append("Not done (the first step failed):")
        lines += [f"  - {p}" for p in self.pending]
        if self.attention:
            lines.append("Needs attention:")
            lines += [f"  - {a}" for a in self.attention]
        if fix:
            lines.append(fix)
        return "\n".join(lines)


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
    document_request = e.method == "GET" and re.fullmatch(r"docs/[^/]+/tables", e.path or "")
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
    if column.type == "Choice":
        choices = column.choices if column.choices is not None else schema.DEFAULT_GRADE_CHOICES
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
        return f"Create table {self.table.id} ({len(self.columns)} columns)"

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
        return f"Add missing columns to {self.table}: {self._names()}"

    def created(self) -> str:
        return f"Added columns to {self.table}: {self._names()}"

    def tables(self) -> tuple[str, ...]:
        return (self.table,)

    def _names(self) -> str:
        return ", ".join(c.id for c in self.columns)

    def apply(self, client: GristClient) -> None:
        sent = [c.id for c in self.columns]
        triggers = [c for c in self.columns if c.recalc_deps]
        refs = {i: c["fields"]["colRef"] for i, c in _refs(client, self.table).items()}
        _check_ids(sent, client.add_columns(self.table, [column_payload(c) for c in self.columns]))
        if triggers:
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
        return f"Show names instead of ids in {self.table}: {self._names()}"

    def created(self) -> str:
        return f"Set {self.table} columns to show names: {self._names()}"

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
        return (
            f"Create the {s.page!r} page with summary table {s.id} "
            f"(grouped by {', '.join(s.group_by)})"
        )

    def created(self) -> str:
        return f"Created the {self.summary.page!r} page with summary table {self.summary.id}"

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
) -> tuple[tuple[str, str, str], ...]:
    names = _event_names(events)
    have = {
        (_norm(names.get(r["fields"].get("Event"))), _norm(r["fields"].get("Grade")))
        for r in implements
    }
    return tuple(i for i in schema.EXAMPLE_IMPLEMENTS if (_norm(i[0]), _norm(i[1])) not in have)


@dataclass(frozen=True)
class SeedEvents(Step):
    """Add the example Events whose Name is not there yet."""

    def describe(self) -> str:
        return "Add the example Events that are missing (matched by Name)"

    def created(self) -> str:
        return "Added the example Events"

    def tables(self) -> tuple[str, ...]:
        return ("Events",)

    def apply(self, client: GristClient) -> None:
        # Re-read: the plan was shown before confirmation, and Grist can be edited meanwhile.
        if rows := _missing_events(client.records("Events")):
            client.add("Events", [{"Name": n, "Kind": k} for n, k in rows])


@dataclass(frozen=True)
class SeedImplements(Step):
    """Add the example Implements whose Event and Grade pair is not there yet."""

    def describe(self) -> str:
        return "Add the example Implements that are missing (matched by Event and Grade)"

    def created(self) -> str:
        return "Added the example Implements"

    def tables(self) -> tuple[str, ...]:
        return ("Implements",)

    def apply(self, client: GristClient) -> None:
        events = client.records("Events")
        rows = _missing_implements(events, client.records("Implements"))
        ids = {}
        for i, name in _event_names(events).items():
            ids.setdefault(_norm(name), i)
        add = []
        for event, grade, weight in rows:
            if _norm(event) not in ids:
                raise InitError(f"Events has no row named {event!r}; cannot add its Implements")
            add.append({"Event": ids[_norm(event)], "Grade": grade, "Weight": weight})
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

    Results go in with Athlete and Event in one call, so Grade and Implement resolve from
    their trigger formulas. Needs the example Events (and Implements) from the steps before.
    """

    def describe(self) -> str:
        return (
            f"Add demo data: {len(schema.DEMO_ATHLETES)} athletes, {len(schema.DEMO_MEETS)} "
            f"meets, {len(schema.DEMO_RESULTS)} results (only if those tables are empty)"
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
                client.add("Athletes", [{"Name": n, "Grade": g} for n, g in schema.DEMO_ATHLETES]),
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

    def unchanged(self) -> list[str]:
        """Present tables that no step touches: they existed and needed nothing."""
        touched = {t for s in self.steps for t in s.tables()}
        return [t for t in self.present if t not in touched]

    notes: list[str] = field(default_factory=list)  # reported, never acted on
    conflicts: list[str] = field(default_factory=list)  # case-only id clashes: the run fails


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
) -> Plan:
    """Diff the contract against `actual`; additions only.

    `actual` maps table id -> {column id: column fields} for the contract tables and the
    summary table that are in the document. `pages` are the page names that exist and
    `summary_tables` the ids of real summary tables. `events` and `implements` are the
    existing rows of those tables (None: the table does not exist yet). Missing example
    Events are always planned, missing example Implements only if `seed_implements`.
    `demo_data` adds the demo step last; the caller checks that the tables are empty.

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
        _plan_seeds(plan, events or [], implements or [], seed_implements or demo_data)
        if demo_data:
            plan.steps.append(LoadDemoData())
    return plan


def _plan_seeds(
    plan: Plan, events: Sequence[Mapping], implements: Sequence[Mapping], implements_too: bool
) -> None:
    missing_events = _missing_events(events)
    if missing_events:
        plan.steps.append(SeedEvents())
    # The Events rows added above are new, so they cannot have Implements yet.
    if implements_too and (missing_events or _missing_implements(events, implements)):
        plan.steps.append(SeedImplements())


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
    page_exists = s.page in pages
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


# Workspace lookup.


def resolve_workspace(client: GristClient, workspace: str, org: str | None = None) -> int:
    """Return a workspace id from a numeric id or a name (optionally within one org)."""
    if workspace.isascii() and workspace.isdigit():
        return int(workspace)
    orgs = client.orgs()
    if org is not None:
        orgs = [o for o in orgs if str(o["id"]) == org or o.get("name") == org]
        if not orgs:
            raise InitError(
                f"Organisation {org!r} not found. Check --org (an id or exact name) and that "
                "the API key's user can see it."
            )
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
    return int(matches[0]["id"])


# CLI.


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bestyet-init",
        description="Add the BestYet tables and columns to a Grist document. Only adds.",
    )
    p.add_argument("--create", action="store_true", help="create a new empty document first")
    p.add_argument("--workspace", help="workspace id or name (with --create)")
    p.add_argument("--org", help="organisation id or name, to narrow --workspace")
    p.add_argument("--name", default="BestYet", help="name of the new document (default BestYet)")
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument(
        "--check",
        action="store_true",
        help="read-only: report every difference from the contract, exit non-zero on any",
    )
    p.add_argument(
        "--seed-implements",
        action="store_true",
        help="also add the example Implements rows (Events are always seeded); only adds",
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
            "also load fictional athletes, meets and results (and the example Implements); "
            "refused unless Athletes, Meets and Results are all empty"
        ),
    )
    args = p.parse_args(argv)
    if args.check:
        used = [
            flag
            for flag, on in (
                ("--create", args.create),
                ("--workspace", args.workspace),
                ("--org", args.org),
                ("--dry-run", args.dry_run),
                ("--seed-implements", args.seed_implements),
                ("--demo-data", args.demo_data),
                ("--allow-existing-tables", args.allow_existing_tables),
                ("--yes", args.yes),
            )
            if on
        ]
        if used:
            p.error(f"--check is read-only and cannot be combined with {', '.join(used)}")
    if args.create and not args.workspace:
        p.error("--create needs --workspace")
    if not args.create and (args.workspace or args.org):
        p.error("--workspace and --org only apply with --create")
    return args


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


def run(
    args: argparse.Namespace,
    config: Config,
    out,
    transport: httpx.BaseTransport | None = None,
) -> None:
    def client_for(doc_id: str) -> GristClient:
        return GristClient(config.grist_url, doc_id, config.grist_api_key, transport=transport)

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
        with _explaining(config, "document"), client_for(config.grist_doc_id) as client:
            _set_up_existing(client, config, args, out)
        return

    with _explaining(config, "workspace"), client_for("") as admin:
        workspace_id = resolve_workspace(admin, args.workspace, args.org)
    lead = [
        f"Create document {args.name!r} in workspace {workspace_id}",
        (
            f"Then remove Grist's default table {DEFAULT_TABLE} if it is untouched "
            "(no rows, only columns A, B, C)"
        ),
    ]
    # A new document is empty apart from that default table, so the plan is the full contract.
    plan = build_plan({}, seed_implements=args.seed_implements, demo_data=args.demo_data)
    if not _show_plan(plan, lead, args, out) or args.dry_run or not _confirm(args, out):
        return
    with _explaining(config, "workspace"), client_for("") as admin:
        doc_id = admin.create_doc(workspace_id, args.name)
    print(f"Created document {args.name!r} (id {doc_id}).", file=out)
    done = [f"Created document {args.name!r} (id {doc_id})"]
    try:
        with client_for(doc_id) as client:
            removed, warning = remove_default_table(client)
            if removed:
                done.append(f"Removed Grist's empty default table {DEFAULT_TABLE}")
            _apply(plan, client, [warning] if warning else [], args, out, done, doc_id)
    except PartialFailure as e:
        raise InitError(e.report(config, rerun_hint(doc_id))) from None
    except GristError as e:
        raise InitError(f"{explain(e, config, 'document')}\n{rerun_hint(doc_id)}") from None
    except InitError as e:
        raise InitError(f"{e}\n{rerun_hint(doc_id)}") from None
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
    )
    if not _show_plan(plan, [], args, out) or args.dry_run or not _confirm(args, out):
        _finish(plan, warnings, args, out)
        return
    try:
        _apply(plan, client, warnings, args, out)
    except PartialFailure as e:
        fix = "Fix the cause, then re-run: it only adds what is missing." if e.rerun_safe else ""
        raise InitError(e.report(config, fix)) from None
    except KeyboardInterrupt:
        raise Interrupted("aborted; re-run to add what is still missing") from None


def _show_plan(plan: Plan, lead: Sequence[str], args: argparse.Namespace, out) -> bool:
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
    lines = [*lead, *(s.describe() for s in plan.steps)]
    if not lines:
        print(
            "Nothing to do: the document already has everything this script adds. "
            "To verify it against the contract, run bestyet-init --check.",
            file=out,
        )
        return False
    print("Plan:", file=out)
    for line in lines:
        print(f"  - {line}", file=out)
    if args.dry_run:
        print("Dry run: nothing was changed. Run again without --dry-run to apply.", file=out)
    return True


def _confirm(args: argparse.Namespace, out) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise InitError("not interactive; pass --yes to run non-interactively")
    out.write("Apply these changes? [y/N] ")
    out.flush()
    try:
        answer = sys.stdin.readline()
    except KeyboardInterrupt:
        print(file=out)
        raise Interrupted("aborted; nothing was changed") from None
    if not answer:
        print(file=out)  # EOF: end the prompt line
    if answer.strip().lower() in ("y", "yes"):
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
) -> None:
    """Apply the steps. `done` holds what was already done in this run (the document)."""
    done = list(done)
    print(f"Applying {len(plan.steps)} change{'' if len(plan.steps) == 1 else 's'}...", file=out)
    for i, step in enumerate(plan.steps):
        try:
            step.apply(client)
        except (GristError, InitError) as e:
            attention = [f"{w}. To fix: {next_step(args.allow_existing_tables)}" for w in warnings]
            raise PartialFailure(
                done, [s.describe() for s in plan.steps[i:]], e, attention
            ) from None
        done.append(step.created())
    _finish(plan, warnings, args, out, done, doc_id)


def _finish(
    plan: Plan,
    warnings: Sequence[str],
    args: argparse.Namespace,
    out,
    done: Sequence[str] | None = None,
    doc_id: str | None = None,
) -> None:
    """Print the closing summary (after a run that applied changes), warnings and next steps.

    `done` is None when nothing was applied (dry run, declined, nothing to do)."""
    if done is not None:
        print("\nSummary", file=out)
        print("  Done:" if done else "  Done: nothing", file=out)
        for item in done:
            print(f"    - {item}", file=out)
        if unchanged := plan.unchanged():
            print(f"  Already there, unchanged: tables {', '.join(unchanged)}", file=out)
    if warnings:
        print("Needs attention:", file=out)
        for w in warnings:
            print(f"  - {w}. To fix: {next_step(args.allow_existing_tables)}", file=out)
    if plan.conflicts:
        raise InitError(
            "The document differs from the contract in ways this script does not change "
            "(listed above). Fix them in Grist, then re-run."
        )
    if done is not None:
        print("\nNext steps", file=out)
        if doc_id:
            print(
                "  - Add this line to your environment or .env (this script saved nothing):",
                file=out,
            )
            print(f"      GRIST_DOC_ID={doc_id}", file=out)
        print(
            "  - Run bestyet-init --check to confirm the document matches the contract.", file=out
        )


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    args = parse_args(argv)
    config = None
    try:
        config = Config.from_env(environ, require_doc_id=not args.create)
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
    except InitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - no traceback (with request details) on the terminal
        print(f"error: unexpected failure ({type(e).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
