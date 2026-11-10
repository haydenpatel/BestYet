"""Init script: set up a Grist document so the schema contract is satisfied.

Run as ``python -m bestyet.init_grist`` (or ``bestyet-init``). It builds a plan by diffing
``schema.TABLES`` against the document, then applies only additions through the REST API:
missing tables, columns (plain, formula and trigger), the PB summary table with its page, and
``visibleCol`` on Ref columns where it is unset. It never drops, renames, retypes or
overwrites; the one exception is Grist's own empty default table in a document this run
created (see ``remove_default_table``). The whole plan is printed first and applied only after confirmation
(``--yes`` skips the prompt; without a terminal ``--yes`` or ``--dry-run`` is required).

The plan is a list of small steps, each with ``describe()`` and ``apply(client)``, so later
work (seed rows) adds step types and extends ``build_plan`` without touching the CLI.
"""

import argparse
import json
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

import httpx

from bestyet import schema
from bestyet.config import Config, ConfigError
from bestyet.grist import GristClient, GristError


class InitError(Exception):
    """A failure with a message that is safe to print as is."""


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
        raise NotImplementedError

    def apply(self, client: GristClient) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class CreateTable(Step):
    table: schema.Table
    columns: tuple[schema.Column, ...]

    def describe(self) -> str:
        names = ", ".join(c.id for c in self.columns)
        return f"create table {self.table.id} ({names})"

    def apply(self, client: GristClient) -> None:
        sent = [self.table.id]
        _check_ids(sent, client.add_tables([table_payload(self.table, self.columns)]))


@dataclass(frozen=True)
class AddColumns(Step):
    table: str
    columns: tuple[schema.Column, ...]

    def describe(self) -> str:
        names = ", ".join(c.id for c in self.columns)
        return f"add columns to {self.table}: {names}"

    def apply(self, client: GristClient) -> None:
        sent = [c.id for c in self.columns]
        triggers = [c for c in self.columns if c.recalc_deps]
        refs = {i: c["fields"]["colRef"] for i, c in _refs(client, self.table).items()}
        _check_ids(sent, client.add_columns(self.table, [column_payload(c) for c in self.columns]))
        if triggers:
            # A reference list is sent as ["L", ref, ...]; the column API wants it in an update.
            client.update_columns(
                self.table,
                [
                    {"id": c.id, "fields": {"recalcDeps": ["L", *(refs[d] for d in c.recalc_deps)]}}
                    for c in triggers
                ],
            )


@dataclass(frozen=True)
class SetVisibleCols(Step):
    """Show names instead of row ids in Ref columns: set `visibleCol` where it is unset."""

    table: str
    items: tuple[tuple[str, str, str], ...]  # (column id, target table id, target column id)

    def describe(self) -> str:
        return f"show names in {self.table} Ref columns: {', '.join(i[0] for i in self.items)}"

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
            f"create page {s.page!r} showing summary table {s.id} "
            f"(grouped by {', '.join(s.group_by)})"
        )

    def apply(self, client: GristClient) -> None:
        s = self.summary
        refs = _refs(client, s.source_table)
        table_ref = next(
            t["fields"]["tableRef"] for t in client.tables() if t["id"] == s.source_table
        )
        group = [refs[g]["fields"]["colRef"] for g in s.group_by]
        result = client.apply([["CreateViewSection", table_ref, 0, "record", group, None]])
        view_ref = result["retValues"][0]["viewRef"]
        client.apply([["UpdateRecord", "_grist_Views", view_ref, {"name": s.page}]])
        if not any(t["id"] == s.id for t in client.tables()):
            raise InitError(f"Grist did not create {s.id}; the document needs a manual fix")


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # reported, never acted on
    conflicts: list[str] = field(default_factory=list)  # case-only id clashes: the run fails


# Contract parts that later issues implement; named in the output so they are not silent.
DEFERRED = ("seed rows",)


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
                f"contract says {c.type}; left as is"
            )
    return missing


def build_plan(
    actual: Mapping[str, Mapping[str, Mapping]],
    pages: Collection[str] = (),
    summary_tables: Collection[str] = (),
) -> Plan:
    """Diff the contract against `actual`; additions only.

    `actual` maps table id -> {column id: column fields} for the contract tables and the
    summary table that are in the document. `pages` are the page names that exist and
    `summary_tables` the ids of real summary tables.

    Grist keeps ids unique ignoring case, so ids are matched ignoring case. A case-only
    difference is never added or renamed: it is a conflict the owner must fix in Grist.
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
    return plan


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
            raise InitError(f"organisation {org!r} not found")
    matches = [w for o in orgs for w in client.workspaces(o["id"]) if w.get("name") == workspace]
    if not matches:
        raise InitError(f"workspace {workspace!r} not found")
    if len(matches) > 1:
        ids = ", ".join(str(w["id"]) for w in matches)
        raise InitError(
            f"workspace name {workspace!r} is ambiguous (ids: {ids}); use the id or --org"
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
        "--allow-existing-tables",
        action="store_true",
        help="proceed even if the document has tables that are not part of the contract",
    )
    p.add_argument(
        "-y", "--yes", action="store_true", help="apply without asking (for non-interactive use)"
    )
    args = p.parse_args(argv)
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
        f"Document {doc_id} was created; re-run without --create using GRIST_DOC_ID={doc_id} "
        f"(add --allow-existing-tables if {DEFAULT_TABLE} is still there)"
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

    if not args.create:
        if not config.grist_doc_id:
            raise InitError("GRIST_DOC_ID is missing (or use --create)")
        with client_for(config.grist_doc_id) as client:
            _set_up_existing(client, args, out)
        return

    with client_for("") as admin:
        workspace_id = resolve_workspace(admin, args.workspace, args.org)
    lead = [
        f"create document {args.name!r} in workspace {workspace_id}",
        (
            f"then, if Grist's default table {DEFAULT_TABLE} is untouched and empty "
            "(only columns A, B, C, no rows), remove it"
        ),
    ]
    # A new document is empty apart from that default table, so the plan is the full contract.
    plan = build_plan({})
    if not _show_plan(plan, lead, args, out) or args.dry_run or not _confirm(args, out):
        return
    with client_for("") as admin:
        doc_id = admin.create_doc(workspace_id, args.name)
    print(f"done: created document {args.name!r}", file=out)
    print(f"document id: {doc_id}", file=out)
    print("put it in GRIST_DOC_ID; it is not saved anywhere", file=out)
    try:
        with client_for(doc_id) as client:
            removed, warning = remove_default_table(client)
            if removed:
                print(f"done: removed Grist's default table {DEFAULT_TABLE}", file=out)
            if warning:
                print(f"warning: {warning}", file=out)
            _apply(plan, client, [warning] if warning else [], args, out)
    except (GristError, InitError) as e:
        raise InitError(f"{e}. {rerun_hint(doc_id)}") from None
    except KeyboardInterrupt:
        raise Interrupted(f"aborted. {rerun_hint(doc_id)}") from None


def _set_up_existing(client: GristClient, args: argparse.Namespace, out) -> None:
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
            "document has tables that are not part of BestYet ("
            + ", ".join(extra)
            + "); pass --allow-existing-tables to add to it anyway"
            + hint
        )
    warnings = [not_created_warning()] if DEFAULT_TABLE in extra else []
    present = contract_table_ids(tables)
    actual = {t: {c["id"]: c.get("fields", {}) for c in client.columns(t)} for t in present}
    summary_tables = [t["id"] for t in tables if t.get("fields", {}).get("summarySourceTable")]
    pages = [
        v["fields"]["name"]
        for v in client.records("_grist_Views")
        if v["fields"].get("type") != "raw_data"
    ]
    plan = build_plan(actual, pages, summary_tables)
    for w in warnings:
        print(f"warning: {w}", file=out)
    if not _show_plan(plan, [], args, out) or args.dry_run or not _confirm(args, out):
        _finish(plan, warnings, args, out)
        return
    try:
        _apply(plan, client, warnings, args, out)
    except KeyboardInterrupt:
        raise Interrupted("aborted; re-run to add what is still missing") from None


def _show_plan(plan: Plan, lead: Sequence[str], args: argparse.Namespace, out) -> bool:
    """Print notes, conflicts and the whole plan. Returns False when there is nothing to do."""
    for note in plan.notes:
        print(f"note: {note}", file=out)
    for conflict in plan.conflicts:
        print(f"warning: {conflict}; left untouched", file=out)
    lines = [*lead, *(s.describe() for s in plan.steps)]
    if not lines:
        print("nothing to do: the document already has everything this script adds", file=out)
        return False
    print("plan:", file=out)
    for line in lines:
        print(f"  - {line}", file=out)
    if args.dry_run:
        print("dry run: nothing was changed", file=out)
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
    print("not applied: nothing was changed", file=out)
    return False


def _apply(
    plan: Plan, client: GristClient, warnings: Sequence[str], args: argparse.Namespace, out
) -> None:
    for step in plan.steps:
        step.apply(client)
        print(f"done: {step.describe()}", file=out)
    _finish(plan, warnings, args, out)


def _finish(plan: Plan, warnings: Sequence[str], args: argparse.Namespace, out) -> None:
    print(f"not handled by this script yet: {', '.join(DEFERRED)}", file=out)
    for w in warnings:
        print(f"warning: {w}; next step: {next_step(args.allow_existing_tables)}", file=out)
    if plan.conflicts:
        raise InitError("document differs from the contract; fix it in Grist")


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    args = parse_args(argv)
    try:
        config = Config.from_env(environ, require_doc_id=not args.create)
        run(args, config, sys.stdout, transport)
    except Interrupted as e:
        print(f"error: {e}", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print("error: aborted", file=sys.stderr)
        return 130
    except (ConfigError, InitError, GristError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - no traceback (with request details) on the terminal
        print(f"error: unexpected failure ({type(e).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
