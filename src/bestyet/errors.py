"""Readable error pages: a failed Grist call and not found. Never a traceback, headers or keys."""

from flask import Flask, render_template
from werkzeug.exceptions import HTTPException

from bestyet.config import Config
from bestyet.grist import GristError
from bestyet.init_errors import explain

NOT_FOUND = "That page does not exist."
FAILED = "Something went wrong. Try again."


def grist_message(e: GristError, config: Config) -> str:
    """A message safe to show for a failed Grist call: names the host only."""
    if e.status == 404:
        return "Grist could not find something the app asked for (HTTP 404). Check the document."
    return explain(e, config)


def register(app: Flask, config: Config) -> None:
    def page(message: str, status: int):
        return render_template("error.html", message=message, status=status), status

    @app.errorhandler(GristError)
    def grist_failed(e: GristError):
        app.logger.warning("Grist request failed: %s", e)  # a GristError never holds the key
        return page(grist_message(e, config), 502)

    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException):
        return page(NOT_FOUND if e.code == 404 else e.description or FAILED, e.code or 500)

    @app.errorhandler(Exception)
    def failed(e: Exception):
        app.logger.exception("Unhandled error")
        return page(FAILED, 500)
