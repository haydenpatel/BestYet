import guard
import httpx
import pytest

BASE = "https://grist.example.test"


def transport(name=None, status=200, raw=None):
    seen = []

    def handler(request):
        seen.append(request)
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json={"id": "DOC1", "name": name})

    return httpx.MockTransport(handler), seen


@pytest.mark.parametrize(
    "name,expected",
    [
        ("BestYet TEST", True),
        ("TEST", True),
        ("my-TEST-doc", True),
        ("BestYet", False),
        ("test", False),
        ("Tes t", False),
        ("", False),
        (None, False),
        (5, False),
    ],
)
def test_is_test_name(name, expected):
    assert guard.is_test_name(name) is expected


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), (" Yes ", True), ("0", False), ("", False), (None, False)],
)
def test_integration_enabled(value, expected):
    environ = {} if value is None else {guard.ENABLE_VAR: value}
    assert guard.integration_enabled(environ) is expected


def test_allows_test_document_and_reads_only():
    t, seen = transport("Demo TEST")
    assert guard.require_test_document(BASE, "DOC 1", "k", t) == "Demo TEST"
    assert [(r.method, r.url.raw_path.decode()) for r in seen] == [("GET", "/api/docs/DOC%201")]


def test_refuses_other_document():
    t, _ = transport("Demo")
    with pytest.raises(guard.GuardError, match="does not contain"):
        guard.require_test_document(BASE, "DOC1", "k", t)


@pytest.mark.parametrize("status,raw", [(404, None), (500, None), (200, b"not json"), (200, b"[]")])
def test_refuses_when_name_unreadable(status, raw):
    t, _ = transport("Demo TEST", status=status, raw=raw)
    with pytest.raises(guard.GuardError, match="could not read"):
        guard.require_test_document(BASE, "DOC1", "k", t)


def test_refuses_when_name_missing():
    t, _ = transport(None)
    with pytest.raises(guard.GuardError):
        guard.require_test_document(BASE, "DOC1", "k", t)


def test_unreachable_is_refused():
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(guard.GuardError, match="could not reach") as info:
        guard.require_test_document(BASE, "DOC1", "secret-key", httpx.MockTransport(boom))
    assert "secret-key" not in str(info.value)
    assert info.value.__context__ is None or "secret-key" not in repr(info.value.__context__)


def test_error_text_has_no_key_or_url():
    t, _ = transport("Demo")
    with pytest.raises(guard.GuardError) as info:
        guard.require_test_document(BASE, "DOC1", "secret-key", t)
    assert "secret-key" not in str(info.value) and "grist.example" not in str(info.value)
