"""Init script: set up a Grist document so the schema contract is satisfied.

Run as ``python -m bestyet.init_grist`` (or ``bestyet-init``). It builds a plan by diffing
``schema.TABLES`` against the document, then applies only additions through the REST API:
missing tables and missing plain columns. It never drops, renames, retypes or overwrites; the
one exception is Grist's own empty default table in a document this run created (see
``remove_default_table``). The whole plan is printed first and applied only after confirmation
(``--yes`` skips the prompt; without a terminal ``--yes`` or ``--dry-run`` is required).

The plan is a list of small steps, each with ``describe()`` and ``apply(client)``, so later
work (formula and trigger columns, the summary table, seed rows) adds step types and
extends ``build_plan`` without touching the CLI.
"""

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
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
    fields: dict = {"type": column.type}
    if column.type == "Choice":
        choices = column.choices if column.choices is not None else schema.DEFAULT_GRADE_CHOICES
        fields["widgetOptions"] = json.dumps({"choices": list(choices)})
    return {"id": column.id, "fields": fields}


def table_payload(table: schema.Table, columns: Sequence[schema.Column]) -> dict:
    return {"id": table.id, "columns": [column_payload(c) for c in columns]}


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
        _check_ids(sent, client.add_columns(self.table, [column_payload(c) for c in self.columns]))


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # reported, never acted on
    conflicts: list[str] = field(default_factory=list)  # case-only id clashes: the run fails


# Contract parts that later issues implement; named in the output so they are not silent.
DEFERRED = (
    "formula and trigger columns",
    "the summary table",
    "seed rows",
)


def _is_plain(column: schema.Column) -> bool:
    return column.formula is None


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
    known = {t.id.lower() for t in schema.TABLES}
    return [t["id"] for t in actual_tables if t["id"].lower() in known]


def build_plan(actual: Mapping[str, Mapping[str, str]]) -> Plan:
    """Diff the contract against `actual` (table id -> {column id: type}); additions only.

    Grist keeps ids unique ignoring case, so ids are matched ignoring case. A case-only
    difference is never added or renamed: it is a conflict the owner must fix in Grist.
    """
    plan = Plan()
    tables = {t.lower(): t for t in actual}
    for table in schema.TABLES:
        plain = tuple(c for c in table.columns if _is_plain(c))
        actual_id = tables.get(table.id.lower())
        if actual_id is None:
            plan.steps.append(CreateTable(table, plain))
            continue
        if actual_id != table.id:
            plan.conflicts.append(f"table {actual_id!r} exists, contract says {table.id!r}")
            continue
        have = {c.lower(): c for c in actual[actual_id]}
        missing = []
        for c in plain:
            found = have.get(c.id.lower())
            if found is None:
                missing.append(c)
            elif found != c.id:
                plan.conflicts.append(
                    f"{table.id}: column {found!r} exists, contract says {c.id!r}"
                )
            elif actual[actual_id][found] != c.type:
                plan.notes.append(
                    f"{table.id}.{c.id} has type {actual[actual_id][found]}, "
                    f"contract says {c.type}; left as is"
                )
        if missing:
            plan.steps.append(AddColumns(table.id, tuple(missing)))
    return plan


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
DEFAULT_TABLE_NEXT_STEP = "remove it in Grist, or pass --allow-existing-tables on re-runs"


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
            _apply(plan, client, [warning] if warning else [], out)
    except (GristError, InitError) as e:
        raise InitError(
            f"{e}. Document {doc_id} was created; re-run without --create "
            f"using GRIST_DOC_ID={doc_id}"
        ) from None


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
    actual = {
        t: {c["id"]: c.get("fields", {}).get("type", "") for c in client.columns(t)}
        for t in contract_table_ids(tables)
    }
    plan = build_plan(actual)
    for w in warnings:
        print(f"warning: {w}", file=out)
    if not _show_plan(plan, [], args, out) or args.dry_run or not _confirm(args, out):
        _finish(plan, warnings, out)
        return
    _apply(plan, client, warnings, out)


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
    if sys.stdin.readline().strip().lower() in ("y", "yes"):
        return True
    print("not applied: nothing was changed", file=out)
    return False


def _apply(plan: Plan, client: GristClient, warnings: Sequence[str], out) -> None:
    for step in plan.steps:
        step.apply(client)
        print(f"done: {step.describe()}", file=out)
    _finish(plan, warnings, out)


def _finish(plan: Plan, warnings: Sequence[str], out) -> None:
    print(f"not handled by this script yet: {', '.join(DEFERRED)}", file=out)
    for w in warnings:
        print(f"warning: {w}; next step: {DEFAULT_TABLE_NEXT_STEP}", file=out)
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
    except (ConfigError, InitError, GristError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - no traceback (with request details) on the terminal
        print(f"error: unexpected failure ({type(e).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
