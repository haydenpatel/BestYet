"""Readable error pages: a failed Grist call and not found. Never a traceback, headers or keys.

The page shows fixed text only, and the log holds the status and cause, not what Grist said:
a reply body could hold anything, and cannot be reliably scrubbed.
"""

from flask import Flask, render_template
from werkzeug.exceptions import HTTPException

from bestyet.config import Config
from bestyet.grist import GristError
from bestyet.init_errors import explain

NOT_FOUND = "That page does not exist."
FAILED = "Something went wrong. Try again."
KEPT_HEADERS = ("Allow", "Retry-After")  # a client needs these with 405, 429 and 503


def grist_message(e: GristError, config: Config) -> str:
    """A message safe to show for a failed Grist call. Names the host at most, never what
    Grist said."""
    if e.status in (0, 401, 403) or e.cause in ("redirect", "invalid_json"):
        return explain(e, config)  # fixed wording, or the host only
    if e.status == 404:
        return "Grist could not find something the app asked for (HTTP 404). Check the document."
    if e.status >= 500:
        return f"Grist reported a server error (HTTP {e.status}). Try again in a moment."
    return f"Grist could not handle the request (HTTP {e.status}). Try again in a moment."


def register(app: Flask, config: Config) -> None:
    def page(message: str, status: int):
        return render_template("error.html", message=message, status=status), status

    @app.errorhandler(GristError)
    def grist_failed(e: GristError):
        app.logger.warning("Grist request failed (status %s, %s)", e.status, e.cause or "http")
        return page(grist_message(e, config), 502)

    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException):
        body, status = page(NOT_FOUND if e.code == 404 else e.description or FAILED, e.code or 500)
        headers = {k: v for k, v in e.get_headers() if k in KEPT_HEADERS}
        return body, status, headers

    @app.errorhandler(Exception)
    def failed(e: Exception):
        if app.testing or app.debug:
            raise e  # show the real traceback in tests and debug runs
        app.logger.exception("Unhandled error")
        return page(FAILED, 500)
