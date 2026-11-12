import json
import socket
import ssl
import traceback

import httpx
import pytest

from bestyet.config import Secret
from bestyet.grist import GristClient, GristError

KEY = "test-key-0123456789"
BASE = "https://grist.example.test"


def make(handler, base=BASE, doc_id="DOC1"):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    client = GristClient(base, doc_id, Secret(KEY), transport=httpx.MockTransport(wrapped))
    return client, calls


def reply(body, status=200):
    return lambda request: httpx.Response(status, json=body)


def test_records_with_filter_and_sort():
    client, calls = make(reply({"records": [{"id": 1, "fields": {"A": 2}}]}))
    assert client.records("T", filter={"A": [2]}, sort="-A") == [{"id": 1, "fields": {"A": 2}}]
    req = calls[0]
    assert (req.method, req.url.path) == ("GET", "/api/docs/DOC1/tables/T/records")
    assert json.loads(req.url.params["filter"]) == {"A": [2]}
    assert req.url.params["sort"] == "-A"
    assert req.headers["Authorization"] == f"Bearer {KEY}"


def test_records_without_params_sends_none():
    client, calls = make(reply({"records": []}))
    client.records("T")
    assert calls[0].url.query == b""


def test_add_returns_ids():
    client, calls = make(reply({"records": [{"id": 5}, {"id": 6}]}))
    assert client.add("T", [{"A": 1}, {"A": 2}]) == [5, 6]
    assert calls[0].method == "POST"
    assert json.loads(calls[0].content) == {"records": [{"fields": {"A": 1}}, {"fields": {"A": 2}}]}


def test_patch():
    client, calls = make(reply({}))
    client.patch("T", [{"id": 5, "fields": {"A": 3}}])
    assert calls[0].method == "PATCH"
    assert json.loads(calls[0].content) == {"records": [{"id": 5, "fields": {"A": 3}}]}


def test_delete_handles_empty_response():
    client, calls = make(lambda r: httpx.Response(200))
    assert client.delete("T", [5, 6]) is None
    assert (calls[0].method, calls[0].url.path) == ("POST", "/api/docs/DOC1/tables/T/data/delete")
    assert json.loads(calls[0].content) == [5, 6]


def test_sql_with_args():
    client, calls = make(reply({"records": [{"fields": {"n": 1}}]}))
    assert client.sql("SELECT ?", [1]) == [{"n": 1}]
    assert calls[0].url.path == "/api/docs/DOC1/sql"
    assert json.loads(calls[0].content) == {"sql": "SELECT ?", "args": [1]}


def test_sql_without_args_omits_them():
    client, calls = make(reply({"records": []}))
    client.sql("SELECT 1")
    assert json.loads(calls[0].content) == {"sql": "SELECT 1"}


def test_schema_reads():
    client, calls = make(reply({"tables": [{"id": "T"}], "columns": [{"id": "A"}]}))
    assert client.tables() == [{"id": "T"}]
    assert client.columns("T") == [{"id": "A"}]
    assert [c.url.path for c in calls] == [
        "/api/docs/DOC1/tables",
        "/api/docs/DOC1/tables/T/columns",
    ]


def test_apply_sends_action_list():
    client, calls = make(reply([{"retValues": []}]))
    actions = [["AddTable", "T", [{"id": "A", "type": "Text"}]]]
    client.apply(actions)
    assert (calls[0].method, calls[0].url.path) == ("POST", "/api/docs/DOC1/apply")
    assert json.loads(calls[0].content) == actions


def test_orgs_workspaces_create_doc():
    client, calls = make(lambda r: httpx.Response(200, json="newdoc" if r.method == "POST" else []))
    client.orgs()
    client.workspaces(3)
    assert client.create_doc(7, "My doc") == "newdoc"
    assert [(c.method, c.url.path) for c in calls] == [
        ("GET", "/api/orgs"),
        ("GET", "/api/orgs/3/workspaces"),
        ("POST", "/api/workspaces/7/docs"),
    ]
    assert json.loads(calls[2].content) == {"name": "My doc"}


def test_base_url_override_and_trailing_slash():
    client, calls = make(reply({"tables": []}), base="http://localhost:8484/")
    client.tables()
    assert str(calls[0].url) == "http://localhost:8484/api/docs/DOC1/tables"


def test_context_manager_closes():
    client, _ = make(reply({"tables": []}))
    with client:
        client.tables()
    with pytest.raises(RuntimeError):
        client.tables()


def assert_no_key(exc: GristError):
    text = " ".join(
        [str(exc), repr(exc), "".join(traceback.format_exception(exc)), repr(exc.__dict__)]
    )
    assert KEY not in text
    assert exc.__cause__ is None
    assert exc.__context__ is None


def failing(client):
    with pytest.raises(GristError) as info:
        client.records("T")
    assert_no_key(info.value)
    return info.value


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (404, {"error": "Table not found 'T'"}, "Table not found 'T'"),
        (500, {"error": "boom"}, "boom"),
        (401, None, "Unauthorized"),
        (500, {"unexpected": 1}, "Internal Server Error"),
    ],
)
def test_http_errors_are_readable(status, body, message):
    def handler(request):
        return httpx.Response(status, json=body) if body else httpx.Response(status, text="x")

    err = failing(make(handler)[0])
    assert (err.status, err.message) == (status, message)
    assert str(err) == f"Grist error {status}: {message}"


def test_connect_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    err = failing(make(handler)[0])
    assert (err.status, err.message) == (0, "Could not reach Grist")


def test_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    err = failing(make(handler)[0])
    assert (err.status, err.message) == (0, "Grist request timed out")


def test_invalid_json_on_success():
    err = failing(make(lambda r: httpx.Response(200, text="<html>"))[0])
    assert err.message == "Grist returned invalid JSON"


def test_repr_and_str_hide_key():
    client, _ = make(reply({}))
    assert KEY not in repr(client) and KEY not in str(client)
    assert "grist.example.test" in repr(client)


@pytest.mark.parametrize("status", [301, 302])
def test_redirect_is_a_grist_error(status):
    def handler(request):
        return httpx.Response(status, headers={"Location": "https://grist.example.test/api/x"})

    err = failing(make(handler)[0])
    assert err.status == status
    assert "Unexpected redirect" in err.message


def test_ids_are_quoted_in_paths():
    client, calls = make(lambda r: httpx.Response(200), doc_id="D/1 x")
    client.delete("a/b c?#", [1])
    client.workspaces("o/1")
    client.create_doc("w 2", "n")
    assert [c.url.raw_path for c in calls] == [
        b"/api/docs/D%2F1%20x/tables/a%2Fb%20c%3F%23/data/delete",
        b"/api/orgs/o%2F1/workspaces",
        b"/api/workspaces/w%202/docs",
    ]


def test_repr_shows_only_scheme_host_port():
    client = GristClient(
        "https://" + "user" + ":" + "pw" + "@grist.example.test:8484/path?q=1",
        "DOC1",
        Secret(KEY),
        transport=httpx.MockTransport(reply({})),
    )
    assert repr(client) == "GristClient(base_url='https://grist.example.test:8484', doc_id='DOC1')"


def _connect_failure(cause):
    def handler(request):
        raise httpx.ConnectError("failed", request=request) from cause

    return failing(make(handler)[0])


@pytest.mark.parametrize(
    ("cause", "kind"),
    [
        (socket.gaierror(8, "no such host"), "dns"),
        (ConnectionRefusedError(61, "refused"), "refused"),
        (ssl.SSLCertVerificationError("bad certificate"), "tls"),
        (OSError("other"), "other"),
    ],
)
def test_unreachable_cause_is_classified(cause, kind):
    err = _connect_failure(cause)
    assert (err.status, err.message, err.cause) == (0, "Could not reach Grist", kind)


def test_timeout_redirect_and_bad_reply_causes():
    def slow(request):
        raise httpx.ConnectTimeout("slow", request=request)

    assert failing(make(slow)[0]).cause == "timeout"
    assert failing(make(lambda r: httpx.Response(301, headers={"location": "/x"}))[0]).cause == (
        "redirect"
    )
    assert failing(make(lambda r: httpx.Response(200, text="<html>"))[0]).cause == "invalid_json"
    assert failing(make(lambda r: httpx.Response(401, json={"error": "no"}))[0]).cause is None
