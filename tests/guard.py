"""Safety guard for tests that write to Grist. Contributor tooling only; the app has no such logic.

A test that writes, deletes or loads demo data must first call `require_test_document`, which
reads the target document's name and refuses unless it contains `TEST`.
"""

import os
from urllib.parse import quote

import httpx

MARKER = "TEST"
ENABLE_VAR = "BESTYET_INTEGRATION"


class GuardError(Exception):
    """The target document is not a TEST document (or its name could not be read)."""


def integration_enabled(environ=None) -> bool:
    """True when the env flag asks for integration tests (`1`, `true` or `yes`)."""
    environ = os.environ if environ is None else environ
    return (environ.get(ENABLE_VAR) or "").strip().lower() in ("1", "true", "yes")


def is_test_name(name: object) -> bool:
    return isinstance(name, str) and MARKER in name


def read_doc_name(base_url: str, doc_id: str, api_key: str, transport=None) -> str:
    """GET the document's name. Errors name no URL, header or key."""
    failure = None
    try:
        with httpx.Client(
            base_url=base_url.rstrip("/") + "/api/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
            transport=transport,
        ) as http:
            response = http.get(f"docs/{quote(doc_id, safe='')}")
    except httpx.HTTPError:
        failure = "could not reach Grist to read the document name"
    if failure:
        raise GuardError(failure)
    if not response.is_success:
        raise GuardError(f"could not read the document name (HTTP {response.status_code})")
    try:
        name = response.json().get("name")
    except (ValueError, AttributeError):
        name = None
    if not isinstance(name, str):
        raise GuardError("could not read the document name")
    return name


def require_test_document(base_url: str, doc_id: str, api_key: str, transport=None) -> str:
    """Return the document name, or raise GuardError unless it contains `TEST`."""
    name = read_doc_name(base_url, doc_id, api_key, transport)
    if not is_test_name(name):
        raise GuardError(f"refusing to write: the document name does not contain {MARKER!r}")
    return name
