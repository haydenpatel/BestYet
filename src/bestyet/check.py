"""Contract check: compare a Grist document with ``schema`` and report every difference.

Read-only. Used by ``bestyet-init --check`` and by the app at startup, so both fail on the
same things. Differences are plain sentences that name the table, column or page.

A table or column whose id differs only in case counts as a difference (Grist ids are case
sensitive in the contract). Tables that are not part of the contract are ignored.
"""

from collections.abc import Collection, Iterable, Mapping

from bestyet import schema
from bestyet.grist import GristClient


class ContractError(Exception):
    """The document does not match the contract. `differences` lists every one."""

    def __init__(self, differences: list[str]):
        self.differences = list(differences)
        super().__init__(format_differences(self.differences))


def format_differences(differences: list[str]) -> str:
    noun = "difference" if len(differences) == 1 else "differences"
    lines = [f"The Grist document does not match the BestYet contract ({len(differences)} {noun}):"]
    lines += [f"  - {d}" for d in differences]
    return "\n".join(lines)


def _find(ids: Collection[str], wanted: str) -> tuple[str | None, str | None]:
    """Return (exact id, case-only match). At most one is set."""
    if wanted in ids:
        return wanted, None
    other = next((i for i in ids if i.lower() == wanted.lower()), None)
    return None, other


def _column_differences(
    where: str,
    column: schema.Column,
    fields: Mapping,
    refs: Mapping[str, Mapping[str, int]],
    expected_type: str,
    out: list[str],
) -> None:
    name = f"{where}.{column.id}"
    if fields.get("type") != expected_type:
        out.append(f"{name} has type {fields.get('type')}, contract says {expected_type}")
    if column.formula is not None:
        if not str(fields.get("formula") or "").strip():
            out.append(f"{name} has no formula")
        elif bool(fields.get("isFormula")) != column.is_formula:
            kind = "a formula column" if column.is_formula else "a trigger-formula column"
            out.append(f"{name} should be {kind}")
        elif column.recalc_deps:
            want = sorted(refs[where][d] for d in column.recalc_deps if d in refs.get(where, {}))
            deps = fields.get("recalcDeps") or []
            have = sorted(d for d in deps if isinstance(d, int))
            if fields.get("recalcWhen") != column.recalc_when or have != want:
                names = ", ".join(column.recalc_deps)
                out.append(f"{name} should recalculate when {names} changes")
    if column.visible_col and column.ref_table:
        want_ref = refs.get(column.ref_table, {}).get(column.visible_col)
        if want_ref is not None and fields.get("visibleCol") != want_ref:
            out.append(f"{name} should show {column.ref_table}.{column.visible_col}")


def find_differences(
    tables: list[Mapping],
    columns: Mapping[str, Mapping[str, Mapping]],
    pages: Collection[str],
) -> list[str]:
    """Pure comparison. `tables` is Grist's table list, `columns` maps table id to
    {column id: fields} for the tables in the document, `pages` are non-raw page names."""
    out: list[str] = []
    ids = {t["id"]: t for t in tables}
    refs = {t: {c: f.get("colRef") for c, f in cols.items()} for t, cols in columns.items()}

    def table_cols(wanted: str) -> Mapping[str, Mapping] | None:
        exact, other = _find(ids, wanted)
        if other:
            out.append(f"table {other!r} exists, contract says {wanted!r}")
        elif exact is None:
            out.append(f"table {wanted!r} is missing")
        return columns.get(exact) if exact else None

    def missing_column(where: str, wanted: str, cols: Mapping[str, Mapping]) -> bool:
        exact, other = _find(cols, wanted)
        if exact:
            return False
        if other:
            out.append(f"{where}: column {other!r} exists, contract says {wanted!r}")
        else:
            out.append(f"{where}: column {wanted!r} is missing")
        return True

    for table in schema.TABLES:
        cols = table_cols(table.id)
        if cols is None:
            continue
        for column in table.columns:
            if not missing_column(table.id, column.id, cols):
                _column_differences(table.id, column, cols[column.id], refs, column.type, out)

    s = schema.SUMMARY
    cols = table_cols(s.id)
    if cols is not None:
        source = next((t for t in tables if t["id"] == s.source_table), None)
        source_ref = (source or {}).get("fields", {}).get("tableRef")
        if not source_ref or ids[s.id].get("fields", {}).get("summarySourceTable") != source_ref:
            out.append(f"table {s.id!r} is not a summary table of {s.source_table!r}")
        results = {c.id: c for c in schema.table(s.source_table).columns}
        for g in s.group_by:
            if not missing_column(s.id, g, cols):
                col = results[g]
                _column_differences(
                    s.id,
                    schema.Column(
                        g, col.type, ref_table=col.ref_table, visible_col=col.visible_col
                    ),
                    cols[g],
                    refs,
                    col.type,
                    out,
                )
        for name in schema.SUMMARY_REQUIRED_COLUMNS:
            if missing_column(s.id, name, cols):
                continue
            column = next((c for c in s.columns if c.id == name), None)
            if column is None:  # Grist's own count column
                _column_differences(s.id, schema.Column(name, "Int"), cols[name], refs, "Int", out)
            else:
                _column_differences(s.id, column, cols[name], refs, column.type, out)
    if not has_page(pages, s.page):
        out.append(f"page {s.page!r} is missing")
    return out


def has_page(pages: Iterable[str], name: str) -> bool:
    """Whether a page called `name` exists: case and surrounding spaces do not matter."""
    wanted = name.strip().casefold()
    return any(str(p or "").strip().casefold() == wanted for p in pages)


def page_names(client: GristClient) -> list[str]:
    """Names of the document's pages (Grist's raw data views are not pages)."""
    views = [v.get("fields") or {} for v in client.records("_grist_Views")]
    return [f["name"] for f in views if f.get("type") != "raw_data" and f.get("name") is not None]


def check_document(client: GristClient) -> list[str]:
    """Read the document (GET only) and return every difference from the contract."""
    tables = client.tables()
    wanted = {t.id.lower() for t in schema.TABLES} | {schema.SUMMARY.id.lower()}
    columns = {
        t["id"]: {c["id"]: c.get("fields", {}) for c in client.columns(t["id"])}
        for t in tables
        if t["id"].lower() in wanted
    }
    return find_differences(tables, columns, page_names(client))


def require_contract(client: GristClient) -> None:
    """Raise ContractError unless the document matches the contract."""
    if differences := check_document(client):
        raise ContractError(differences)
