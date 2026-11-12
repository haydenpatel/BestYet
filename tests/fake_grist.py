"""A small in-memory fake of the Grist REST endpoints the init script uses."""

import json
import re

import httpx

KEY = "test-key-0123456789"
BASE = "https://grist.example.test"
DOC = "DOC_ID_HERE"


def unique_id(wanted, existing):
    """Like Grist: ids are unique ignoring case; a clash gets a numeric suffix (Name2)."""
    taken = {k.lower() for k in existing}
    new, n = wanted, 1
    while new.lower() in taken:
        n += 1
        new = f"{wanted}{n}"
    return new


class FakeGrist:
    def __init__(self, tables=None, orgs=None, workspaces=None, new_doc_tables=None, rows=None):
        # tables: {table id: {column id: fields dict}}; rows: {table id: [record dicts]}
        self.tables = {t: dict(cols) for t, cols in (tables or {}).items()}
        self.rows = {t: list(r) for t, r in (rows or {}).items()}
        # What Grist puts in every new document: an empty Table1 with columns A, B, C.
        self.new_doc_tables = (
            new_doc_tables
            if new_doc_tables is not None
            else {"Table1": {c: {"type": "Any"} for c in "ABC"}}
        )
        self.new_doc_rows: dict[str, list] = {}
        self.actions: list[list] = []
        self.summary_tables: set[str] = set()
        self.views: dict[int, str] = {}  # page (view) ref -> name
        self.refs: dict[tuple[str, str], int] = {}  # (table id, column id) -> colRef
        self.next_ref = 100
        self.display: dict[tuple[str, int], str] = {}  # (table, colRef) -> display formula
        self.orgs = orgs if orgs is not None else [{"id": 1, "name": "Org One"}]
        self.workspaces = (
            workspaces if workspaces is not None else {1: [{"id": 10, "name": "Work"}]}
        )
        self.created_docs: list[tuple[int, str]] = []
        self.writes: list[tuple[str, str]] = []
        self.fail_on: str | None = None  # substring of "METHOD path" to answer with a 500
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {KEY}"
        line = f"{request.method} {request.url.path}"
        if self.fail_on and self.fail_on in line:
            return httpx.Response(500, json={"error": "boom"})
        body = json.loads(request.content) if request.content else None
        if request.method != "GET":
            self.writes.append((request.method, request.url.path))
        path = request.url.path.removeprefix("/api/")
        if line == "GET /api/orgs":
            return httpx.Response(200, json=self.orgs)
        if m := re.fullmatch(r"orgs/(\d+)/workspaces", path):
            return httpx.Response(200, json=self.workspaces.get(int(m[1]), []))
        if m := re.fullmatch(r"workspaces/(\d+)", path):
            found = [w for ws in self.workspaces.values() for w in ws if w["id"] == int(m[1])]
            if not found:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=found[0])
        if m := re.fullmatch(r"workspaces/(\d+)/docs", path):
            self.created_docs.append((int(m[1]), body["name"]))
            for ws in self.workspaces.values():
                for w in ws:
                    if w["id"] == int(m[1]) and "docs" in w:
                        w["docs"].append({"id": "NEWDOC", "name": body["name"]})
            self.tables.update({t: dict(c) for t, c in self.new_doc_tables.items()})
            self.rows.update({t: list(r) for t, r in self.new_doc_rows.items()})
            return httpx.Response(200, json="NEWDOC")
        if m := re.fullmatch(r"docs/([^/]+)/(.*)", path):
            return self.doc(request.method, m[2], body)
        return httpx.Response(404, json={"error": "not found"})

    def doc(self, method, rest, body):
        if rest == "apply" and method == "POST":
            rets = []
            for action in body:
                self.actions.append(action)
                rets.append(None)
                if action[0] == "CreateViewSection":
                    rets[-1] = self.create_view_section(action)
                elif action[0] == "UpdateRecord" and action[1] == "_grist_Views":
                    self.views[action[2]] = action[3]["name"]
                elif action[0] == "SetDisplayFormula":
                    self.display[(action[1], action[3])] = action[4]
                elif action[0] == "RemoveTable":
                    if action[1] not in self.tables:
                        return httpx.Response(400, json={"error": "no such table"})
                    del self.tables[action[1]]
                    self.rows.pop(action[1], None)
                else:
                    return httpx.Response(400, json={"error": "unsupported action"})
            return httpx.Response(200, json={"retValues": rets})
        if rest == "tables/_grist_Views/records":
            return httpx.Response(
                200,
                json={"records": [{"id": i, "fields": {"name": n}} for i, n in self.views.items()]},
            )
        if m := re.fullmatch(r"tables/([^/]+)/records", rest):
            if m[1] not in self.tables:
                return httpx.Response(404, json={"error": "not found"})
            if method == "POST":
                rows = self.rows.setdefault(m[1], [])
                added = []
                for r in body["records"]:
                    rows.append({"id": max((x["id"] for x in rows), default=0) + 1, **r})
                    added.append({"id": rows[-1]["id"]})
                return httpx.Response(200, json={"records": added})
            if method != "GET":
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"records": list(self.rows.get(m[1], []))})
        if rest == "tables" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "tables": [
                        {
                            "id": t,
                            "fields": {
                                "summarySourceTable": self.ref("Results", "")
                                if t in self.summary_tables
                                else 0,
                                "tableRef": self.ref(t, ""),
                            },
                        }
                        for t in self.tables
                    ]
                },
            )
        if rest == "tables" and method == "POST":
            ids = []
            for t in body["tables"]:
                new = unique_id(t["id"], self.tables)
                self.tables[new] = {}
                for c in t["columns"]:
                    self.tables[new][unique_id(c["id"], self.tables[new])] = c["fields"]
                ids.append(new)
            return httpx.Response(200, json={"tables": [{"id": i} for i in ids]})
        if m := re.fullmatch(r"tables/([^/]+)/columns", rest):
            table = self.tables.get(m[1])
            if table is None:
                return httpx.Response(404, json={"error": "no table"})
            if method == "GET":
                cols = [
                    {"id": c, "fields": {**f, "colRef": self.ref(m[1], c)}}
                    for c, f in table.items()
                ]
                return httpx.Response(200, json={"columns": cols})
            if method == "PATCH":
                for c in body["columns"]:
                    table[c["id"]] = {**table[c["id"]], **c["fields"]}
                return httpx.Response(200)
            ids = []
            for c in body["columns"]:
                new = unique_id(c["id"], table)
                table[new] = c["fields"]
                ids.append(new)
            return httpx.Response(200, json={"columns": [{"id": i} for i in ids]})
        return httpx.Response(404, json={"error": "not found"})

    def ref(self, table, column):
        if (table, column) not in self.refs:
            self.next_ref += 1
            self.refs[(table, column)] = self.next_ref
        return self.refs[(table, column)]

    def create_view_section(self, action):
        """CreateViewSection on a source table grouped by columns: a summary table and a page."""
        _, table_ref, view_ref, kind, group, _ = action
        assert view_ref == 0 and kind == "record"
        source = next(t for t in self.tables if self.ref(t, "") == table_ref)
        by = [c for c in self.tables[source] if self.ref(source, c) in group]
        name = f"{source}_summary_{'_'.join(by)}"
        if name not in self.tables:
            self.tables[name] = {c: dict(self.tables[source][c]) for c in by}
            self.tables[name]["count"] = {"type": "Int"}
            self.summary_tables.add(name)
        view = max(self.views, default=0) + 1
        self.views[view] = name.replace("_", " ")
        return {"tableRef": self.ref(name, ""), "viewRef": view, "sectionRef": view}
