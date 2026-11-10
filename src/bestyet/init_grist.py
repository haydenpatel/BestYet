"""Init script: set up a Grist document so the schema contract is satisfied.

Run as ``python -m bestyet.init_grist`` (or ``bestyet-init``). It builds a plan by diffing
``schema.TABLES`` against the document, then applies only additions through the REST API:
missing tables and missing plain columns. It never drops, renames, retypes or overwrites.

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


# REST payloads. Kept small and separate: not yet verified against a live Grist server
# (column `fields`, Choice `widgetOptions` JSON, `Ref:Table` types, the table-create body).


def column_payload(column: schema.Column) -> dict:
    fields: dict = {"type": column.type}
    if column.type == "Choice":
        choices = column.choices if column.choices is not None else schema.DEFAULT_GRADE_CHOICES
        fields["widgetOptions"] = json.dumps({"choices": list(choices)})
    return {"id": column.id, "fields": fields}


def table_payload(table: schema.Table, columns: Sequence[schema.Column]) -> dict:
    return {"id": table.id, "columns": [column_payload(c) for c in columns]}


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
        client.add_tables([table_payload(self.table, self.columns)])


@dataclass(frozen=True)
class AddColumns(Step):
    table: str
    columns: tuple[schema.Column, ...]

    def describe(self) -> str:
        names = ", ".join(c.id for c in self.columns)
        return f"add columns to {self.table}: {names}"

    def apply(self, client: GristClient) -> None:
        client.add_columns(self.table, [column_payload(c) for c in self.columns])


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # reported, never acted on


# Contract parts that later issues implement; named in the output so they are not silent.
DEFERRED = (
    "formula and trigger columns",
    "the summary table",
    "seed rows",
)


def _is_plain(column: schema.Column) -> bool:
    return column.formula is None


def unrelated_tables(actual_tables: Sequence[Mapping]) -> list[str]:
    """Ids of tables that are neither in the contract nor summary tables."""
    known = {t.id for t in schema.TABLES} | {schema.SUMMARY.id}
    return [
        t["id"]
        for t in actual_tables
        if t["id"] not in known
        and not t.get("fields", {}).get("summarySourceTable")
        and not t["id"].startswith("_grist_")
    ]


def build_plan(actual: Mapping[str, Mapping[str, str]]) -> Plan:
    """Diff the contract against `actual` (table id -> {column id: type}); additions only."""
    plan = Plan()
    for table in schema.TABLES:
        plain = tuple(c for c in table.columns if _is_plain(c))
        if table.id not in actual:
            plan.steps.append(CreateTable(table, plain))
            continue
        have = actual[table.id]
        missing = tuple(c for c in plain if c.id not in have)
        if missing:
            plan.steps.append(AddColumns(table.id, missing))
        for c in plain:
            if c.id in have and have[c.id] != c.type:
                plan.notes.append(
                    f"{table.id}.{c.id} has type {have[c.id]}, contract says {c.type}; left as is"
                )
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
    args = p.parse_args(argv)
    if args.create and not args.workspace:
        p.error("--create needs --workspace")
    if not args.create and (args.workspace or args.org):
        p.error("--workspace and --org only apply with --create")
    return args


def run(
    args: argparse.Namespace,
    config: Config,
    out,
    transport: httpx.BaseTransport | None = None,
) -> None:
    def client_for(doc_id: str) -> GristClient:
        return GristClient(config.grist_url, doc_id, config.grist_api_key, transport=transport)

    doc_id = config.grist_doc_id
    if args.create:
        with client_for("") as admin:
            workspace_id = resolve_workspace(admin, args.workspace, args.org)
            if args.dry_run:
                print(f"would create document {args.name!r} in workspace {workspace_id}", file=out)
                # A new document is empty: show the full plan without touching anything.
                _report_and_apply(build_plan({}), None, True, out)
                return
            doc_id = admin.create_doc(workspace_id, args.name)
        print(f"created document {args.name!r}", file=out)
        print(f"document id: {doc_id}", file=out)
        print("put it in GRIST_DOC_ID; it is not saved anywhere", file=out)
    elif not doc_id:
        raise InitError("GRIST_DOC_ID is missing (or use --create)")

    with client_for(doc_id) as client:
        tables = client.tables()
        actual = {
            t["id"]: {c["id"]: c.get("fields", {}).get("type", "") for c in client.columns(t["id"])}
            for t in tables
        }
        extra = unrelated_tables(tables)
        if extra and not args.allow_existing_tables:
            raise InitError(
                "document has tables that are not part of BestYet ("
                + ", ".join(extra)
                + "); pass --allow-existing-tables to add to it anyway"
            )
        _report_and_apply(build_plan(actual), client, args.dry_run, out)


def _report_and_apply(plan: Plan, client: GristClient | None, dry_run: bool, out) -> None:
    for note in plan.notes:
        print(f"note: {note}", file=out)
    if not plan.steps:
        print("nothing to do: the document already has everything this script adds", file=out)
    for step in plan.steps:
        if dry_run:
            print(f"would {step.describe()}", file=out)
        else:
            assert client is not None
            step.apply(client)
            print(f"done: {step.describe()}", file=out)
    print(f"not handled by this script yet: {', '.join(DEFERRED)}", file=out)


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
