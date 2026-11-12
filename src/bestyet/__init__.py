"""BestYet: a small mobile web app in front of a Grist document."""

import click
import httpx
from flask import Flask, render_template

from bestyet.check import ContractError, require_contract
from bestyet.config import Config, ConfigError
from bestyet.grist import GristClient, GristError


class StartupError(click.ClickException):
    """The app cannot start; the message is printed as is (`flask run` shows `Error: ...`)."""


def create_app(
    config: Config | None = None,
    *,
    verify: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> Flask:
    """Create the app.

    With no `config`, it is read from the environment. Unless `verify` is False, the Grist
    document is checked against the schema contract first (read only, the same check as
    `bestyet-init --check`), and a missing or invalid setting or a mismatch raises
    `StartupError` with a message naming every problem. `transport` is for tests.
    """
    try:
        config = config or Config.from_env()
        if verify:
            with GristClient(
                config.grist_url, config.grist_doc_id, config.grist_api_key, transport=transport
            ) as client:
                require_contract(client)
    except (ConfigError, ContractError) as e:
        raise StartupError(str(e)) from None
    except GristError as e:
        raise StartupError(f"Could not check the Grist document: {e}") from None

    app = Flask(__name__)
    app.config["BESTYET"] = config

    @app.get("/health")
    def health():
        return render_template("health.html")

    return app
