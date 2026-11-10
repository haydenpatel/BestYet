"""A small in-memory fake of the Grist REST endpoints the init script uses."""

import json
import re

import httpx

KEY = "test-key-0123456789"
BASE = "https://grist.example.test"
DOC = "DOC_ID_HERE"


class FakeGrist:
    def __init__(self, tables=None, orgs=None, workspaces=None):
        # tables: {table id: {column id: fields dict}}
        self.tables = {t: dict(cols) for t, cols in (tables or {}).items()}
        self.summary_tables: set[str] = set()
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
        if m := re.fullmatch(r"workspaces/(\d+)/docs", path):
            self.created_docs.append((int(m[1]), body["name"]))
            return httpx.Response(200, json="NEWDOC")
        if m := re.fullmatch(r"docs/([^/]+)/(.*)", path):
            return self.doc(request.method, m[2], body)
        return httpx.Response(404, json={"error": "not found"})

    def doc(self, method, rest, body):
        if rest == "tables" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "tables": [
                        {
                            "id": t,
                            "fields": {"summarySourceTable": 5 if t in self.summary_tables else 0},
                        }
                        for t in self.tables
                    ]
                },
            )
        if rest == "tables" and method == "POST":
            for t in body["tables"]:
                if t["id"] in self.tables:
                    return httpx.Response(400, json={"error": "table exists"})
                self.tables[t["id"]] = {c["id"]: c["fields"] for c in t["columns"]}
            return httpx.Response(200, json={"tables": [{"id": t["id"]} for t in body["tables"]]})
        if m := re.fullmatch(r"tables/([^/]+)/columns", rest):
            table = self.tables.get(m[1])
            if table is None:
                return httpx.Response(404, json={"error": "no table"})
            if method == "GET":
                cols = [{"id": c, "fields": f} for c, f in table.items()]
                return httpx.Response(200, json={"columns": cols})
            for c in body["columns"]:
                if c["id"] in table:
                    return httpx.Response(400, json={"error": "column exists"})
                table[c["id"]] = c["fields"]
            return httpx.Response(200, json={"columns": [{"id": c["id"]} for c in body["columns"]]})
        return httpx.Response(404, json={"error": "not found"})
