"""Init script errors and their readable messages (no Grist writes, no output)."""

import re
from contextlib import contextmanager
from urllib.parse import urlsplit

from bestyet.config import Config
from bestyet.grist import GristError


class InitError(Exception):
    """A failure with a message that is safe to print."""


class ErrorReport(InitError):
    """An error text with its own headings, printed as is (no "error:" prefix)."""


_CAUSES = {
    "dns": "the host name could not be resolved (DNS)",
    "refused": "the connection was refused (is the server running, and is the port right?)",
    "timeout": "the connection timed out",
    "tls": "a TLS or certificate problem",
    "other": "the connection failed",
}


def explain(e: GristError, config: Config, what: str = "") -> str:
    """A readable message for a failed Grist call (`what`: "workspace" or "document" pins a
    404). Names the host only: never credentials, path, query or key."""
    if e.status == 0:
        parts = urlsplit(config.grist_url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"  # IPv6
        if parts.port:
            host = f"{host}:{parts.port}"
        return (
            f"Could not reach Grist at {host}: {_CAUSES.get(e.cause or '', _CAUSES['other'])}. "
            "Check GRIST_URL, your network and that the Grist server is up."
        )
    if e.cause in ("redirect", "invalid_json"):
        return (
            "Grist gave an unexpected reply. Check that GRIST_URL is the address of a Grist "
            "server (its final http(s) address, without a path)."
        )
    if e.status == 401:
        return "Grist rejected the API key (HTTP 401). Check GRIST_API_KEY."
    if e.status == 403:
        target = f" the {what}" if what else " it"
        return (
            "Grist denied access (HTTP 403). Check that the API key's user can access"
            + target
            + "."
        )
    if e.status == 404 and what == "workspace":
        return (
            "Grist could not find that workspace (HTTP 404). Check --workspace and that the API "
            "key's user can see it."
        )
    document_request = e.method == "GET" and re.fullmatch(r"docs/[^/]+(/tables)?", e.path or "")
    if e.status == 404 and what == "document" and document_request:
        return (
            "Grist could not find the document (HTTP 404). Check GRIST_DOC_ID and that the API "
            "key's user can access it."
        )
    if e.status == 404:
        return (
            f"Grist could not find something the script asked for (HTTP 404: {e.message}). "
            "The document may have been changed during the run; run bestyet-init --check to see "
            "what it looks like now."
        )
    if e.status >= 500:
        return (
            f"Grist reported a server error (HTTP {e.status}: {e.message}). Try again; "
            "if it persists, check the Grist server."
        )
    return str(e)


@contextmanager
def _explaining(config: Config, what: str):
    """Turn a GristError in the block into an InitError with a readable message."""
    try:
        yield
    except GristError as e:
        raise InitError(explain(e, config, what)) from None


class Interrupted(Exception):
    """The run was interrupted (Ctrl-C); the message is safe to print as is."""
