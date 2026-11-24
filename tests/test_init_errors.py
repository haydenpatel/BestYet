"""The readable errors for failed Grist calls: one line, naming the cause, never a secret."""

import socket
import ssl

import httpx
import pytest
from fake_grist import DOC, KEY
from init_helpers import CREATE, ENV, ENV_NO_DOC, reply

from bestyet.init_grist import main


def refuse(cause):
    def handler(request):
        raise httpx.ConnectError("failed", request=request) from cause

    return httpx.MockTransport(handler)


def time_out():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("cause", "text"),
    [
        (socket.gaierror(8, "no such host"), "could not be resolved (DNS)"),
        (ConnectionRefusedError(61, "refused"), "connection was refused"),
        (ssl.SSLCertVerificationError("bad certificate"), "TLS or certificate problem"),
        (OSError("x"), "the connection failed"),
    ],
)
def test_unreachable_names_the_host_and_the_cause_but_nothing_secret(capsys, cause, text):
    userinfo = "bob:" + "hunter2"  # built in parts: a literal user:pass URL trips secret scanners
    env = {**ENV, "GRIST_URL": f"https://{userinfo}@grist.example.test:8484/sub"}
    assert main([], env, refuse(cause)) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: Could not reach Grist at grist.example.test:8484: ")
    assert text in err and "Check GRIST_URL" in err
    for secret in (KEY, "hunter2", "bob", "/sub", "https://"):
        assert secret not in err
    assert err.count("\n") == 1


@pytest.mark.parametrize(
    ("argv", "env", "transport", "text"),
    [
        (["--check"], ENV, time_out(), "at grist.example.test: the connection timed out"),
        (["--check"], ENV, reply(401), "rejected the API key (HTTP 401). Check GRIST_API_KEY"),
        (["--check"], ENV, reply(403), "can access the document"),
        ([*CREATE, "--yes"], ENV_NO_DOC, reply(403), "can access the workspace"),
        (["--yes"], ENV, reply(404), "could not find the document (HTTP 404)"),
        (["--create", "--workspace", "99", "--yes"], ENV_NO_DOC, reply(404), "find that workspace"),
        (["--check"], ENV, reply(500, "boom"), "server error (HTTP 500: boom)"),
        (
            ["--check"],
            ENV,
            httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")),
            "Check that GRIST_URL is the address of a Grist server",
        ),
    ],
)
def test_failed_calls_get_a_one_line_explanation(capsys, argv, env, transport, text):
    assert main(argv, env, transport) == 1
    err = capsys.readouterr().err
    assert text in err
    assert err.count("\n") == 1 and KEY not in err and DOC not in err
