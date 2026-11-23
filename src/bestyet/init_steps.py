"""Init script write steps: REST payloads, the steps themselves, seeding and Table1 handling.

No questions and no output: a step only reads and writes through the client.
"""

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from bestyet import dates, schema
from bestyet.grist import GristClient, GristError
from bestyet.init_errors import InitError

# REST payloads. Kept small and separate: column `fields`, Choice `widgetOptions` JSON,
# `Ref:Table` types and the table-create body were checked against a live Grist server.


def column_payload(column: schema.Column) -> dict:
    """The REST body for creating one column. `recalcDeps` is left out: Grist rejects it (and
    can corrupt the document) with a new column, so `AddColumns.apply` sets it afterwards."""
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
    def apply(self, client: GristClient) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class CreateTable(Step):
    table: schema.Table
    columns: tuple[schema.Column, ...]

    def apply(self, client: GristClient) -> None:
        sent = [self.table.id]
        payload = {"id": self.table.id, "columns": [column_payload(c) for c in self.columns]}
        _check_ids(sent, client.add_tables([payload]))


@dataclass(frozen=True)
class AddColumns(Step):
    table: str
    columns: tuple[schema.Column, ...]

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
    """Create the summary table through a view section on a new page (Grist keeps a summary
    table only while a view section uses it, and reuses it if it exists). Only runs when the
    page is missing."""

    summary: schema.Summary

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


def _event_ids(events: Sequence[Mapping]) -> dict[str, int]:
    """Normalised Event Name -> row id (the first row when names repeat)."""
    ids: dict[str, int] = {}
    for i, name in _event_names(events).items():
        ids.setdefault(_norm(name), i)
    return ids


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
            r["fields"].get("Grade") or "",
            r["fields"].get("Division") or "",
        )
        for r in implements
    }
    return tuple(i for i in schema.SAMPLE_IMPLEMENTS if (_norm(i[0]), i[1], i[2]) not in have)


@dataclass(frozen=True)
class SeedEvents(Step):
    """Add the default Events (from `names`, or all when None) whose Name is not there yet.
    `add_names`, `have` and `for_demo` only shape the plan's wording."""

    names: tuple[str, ...] | None = None
    add_names: tuple[str, ...] = ()
    have: int = 0
    for_demo: bool = False

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
    `have` only shape the plan's wording."""

    add: int = 0
    have: int = 0

    def apply(self, client: GristClient) -> None:
        events = client.records("Events")
        rows = _missing_implements(events, client.records("Implements"))
        ids = _event_ids(events)
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
    """Add the fictional athletes, meets and results, in one call so Grade, Division and
    Implement resolve from their trigger formulas. Needs the Events it uses from earlier steps."""

    def _counts(self) -> str:
        return (
            f"{len(schema.DEMO_ATHLETES)} athletes, {len(schema.DEMO_MEETS)} meets, "
            f"{len(schema.DEMO_RESULTS)} results"
        )

    def apply(self, client: GristClient) -> None:
        _require_empty(client, DEMO_TABLES)  # re-read: the document can change after the plan
        events = _event_ids(client.records("Events"))
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


def _known_ids() -> set[str]:
    """Lower-cased ids of the contract tables and the PB summary table."""
    return {t.id.lower() for t in schema.TABLES} | {schema.SUMMARY.id.lower()}


def unrelated_tables(actual_tables: Sequence[Mapping]) -> list[str]:
    """Ids of tables that are neither in the contract nor summary tables (ids ignore case)."""
    known = _known_ids()
    return [
        t["id"]
        for t in actual_tables
        if t["id"].lower() not in known
        and not t.get("fields", {}).get("summarySourceTable")
        and not t["id"].startswith("_grist_")
    ]


def contract_table_ids(actual_tables: Sequence[Mapping]) -> list[str]:
    """Ids of the contract tables and the PB summary table that are in the document."""
    known = _known_ids()
    return [t["id"] for t in actual_tables if t["id"].lower() in known]


DEFAULT_TABLE = "Table1"  # Grist puts an empty table with columns A, B, C in every new document


DEFAULT_COLUMNS = frozenset({"A", "B", "C"})


def default_table_state(client: GristClient) -> tuple[bool, str | None]:
    """Return (present, problem): is Table1 there, and why it must not be removed. It may go
    only when it is the only user table, has only Grist's default columns and has no rows."""
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
    """Remove Grist's untouched, empty default table; returns (removed, warning). Only for a
    document this run created; the conditions are checked twice and nothing else is removed."""
    for _ in range(2):
        present, problem = default_table_state(client)
        if not present:
            return False, None
        if problem:
            return False, f"{DEFAULT_TABLE} not removed: {problem}"
    client.apply([["RemoveTable", DEFAULT_TABLE]])
    return True, None
