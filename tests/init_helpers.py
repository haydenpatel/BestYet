"""Shared helpers for the init script tests: fake Grist set-ups, scripted answers, fault injection."""

import io
import sys
from pathlib import Path

import httpx
from fake_grist import BASE, DOC, KEY, FakeGrist

import bestyet
from bestyet import schema
from bestyet.init_grist import main

ENV = {"GRIST_URL": BASE, "GRIST_DOC_ID": DOC, "GRIST_API_KEY": KEY}
ENV_NO_DOC = {k: v for k, v in ENV.items() if k != "GRIST_DOC_ID"}
CREATE = ("--create", "--workspace", "Work")
DEFAULTS = {c: {"type": "Any"} for c in "ABC"}  # the columns of Grist's default table


def source_files():
    """Every Python file under src/bestyet, so new modules are covered."""
    files = sorted(Path(bestyet.__file__).parent.rglob("*.py"))
    assert files
    return files


class Stdin(io.StringIO):
    """Scripted answers; `tty` says whether it looks like a terminal."""

    def __init__(self, text="", tty=True):
        super().__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


class Interrupting(Stdin):
    """Ctrl-C at the first question."""

    def readline(self, *args):
        raise KeyboardInterrupt


def full_tables():
    """A fully initialised document: contract tables, summary table, names shown."""
    tables = {
        t.id: {
            c.id: {"type": c.type, **({"visibleCol": 1} if c.visible_col else {})}
            for c in t.columns
        }
        for t in schema.TABLES
    }
    tables[schema.SUMMARY.id] = {"count": {"type": "Int"}, "Best": {"type": "Numeric"}}
    tables[schema.SUMMARY.id].update(
        {g: {"type": "Ref:X", "visibleCol": 1} for g in schema.SUMMARY.group_by[:2]}
    )
    return tables


def full_fake(tables=None):
    """A fake with a fully initialised document, its example Events and its page."""
    fake = FakeGrist(tables if tables is not None else full_tables())
    if "Events" in fake.tables:
        fake.rows["Events"] = [
            {"id": i, "fields": {"Name": n, "Kind": k}}
            for i, (n, k) in enumerate(schema.EXAMPLE_EVENTS, 1)
        ]
    fake.summary_tables.add(schema.SUMMARY.id)
    fake.views[1] = schema.SUMMARY.page
    return fake


def incomplete_fake(*, table="Meets", column=None):
    """A full document with a table (or one column of a table) missing: something to do."""
    fake = full_fake()
    if column:
        del fake.tables[table][column]
    else:
        del fake.tables[table]
    return fake


def workspace_with(*names):
    """The `workspaces` of a FakeGrist: one workspace, Work, holding documents DOC0, DOC1, ..."""
    docs = [{"id": f"DOC{i}", "name": n} for i, n in enumerate(names)]
    return {1: [{"id": 10, "name": "Work", "docs": docs}]}


def run(fake, *argv, env=ENV, yes=True):
    """Run the CLI. Adds --yes (the prompt has its own tests) unless yes=False."""
    if yes:
        argv += ("--yes",)
    return main(list(argv), env, fake.transport)


def answers_from(monkeypatch, text, tty=True):
    """Script stdin only: stdout stays captured, so the guided mode does not start."""
    monkeypatch.setattr(sys, "stdin", Stdin(text, tty=tty))


def terminal(monkeypatch, text="", tty=True):
    """Make stdin (with scripted `text`) and stdout look like a terminal (or not)."""
    monkeypatch.setattr(sys, "stdin", Stdin(text, tty=tty))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty, raising=False)


def guided(monkeypatch, fake, answers, *argv, env=ENV_NO_DOC):
    """Run the CLI in a terminal with scripted answers (no argv: the guided start)."""
    terminal(monkeypatch, answers)
    return main(list(argv), env, fake.transport)


def intercept(fake, hook):
    """Route requests through hook(request) first: a response replaces the fake's answer."""
    orig = fake.handle

    def handle(request):
        response = hook(request)
        return orig(request) if response is None else response

    fake.transport = httpx.MockTransport(handle)


def server_error(fake, when):
    """Answer HTTP 500 to the requests for which when(request) is true."""
    intercept(fake, lambda r: httpx.Response(500, json={"error": "boom"}) if when(r) else None)


def before_nth_get(fake, suffix, n, action):
    """Call action() just before the nth GET of a path ending in suffix is answered."""
    seen = []

    def hook(request):
        if request.method == "GET" and request.url.path.endswith(suffix):
            seen.append(1)
            if len(seen) == n:
                action()

    intercept(fake, hook)


def reply(status, error="denied"):
    """A transport answering every request with an HTTP error."""
    return httpx.MockTransport(lambda request: httpx.Response(status, json={"error": error}))


def event_names(fake):
    return [r["fields"]["Name"] for r in fake.rows.get("Events", [])]


def implement_rows(fake, samples):
    """Implements rows for the given sample tuples, resolving the Event ids in the fake."""
    ids = {r["fields"]["Name"]: r["id"] for r in fake.rows["Events"]}
    return [
        {"id": i, "fields": {"Event": ids[e], "Grade": g, "Division": d, "Weight": w}}
        for i, (e, g, d, w) in enumerate(samples, 1)
    ]
