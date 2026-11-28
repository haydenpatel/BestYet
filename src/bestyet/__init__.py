"""BestYet: a small mobile web app in front of a Grist document."""

import click
import httpx
from flask import Flask, render_template, request

from bestyet import dates, errors, marks, pbs
from bestyet.check import FIX_HINT, ContractError, require_contract
from bestyet.config import Config, ConfigError
from bestyet.data import Data, current_data
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
    except ConfigError as e:
        raise StartupError(str(e)) from None
    # One client for the app's lifetime; it makes no request until it is used.
    client = GristClient(
        config.grist_url, config.grist_doc_id, config.grist_api_key, transport=transport
    )
    try:
        if verify:
            require_contract(client)
    except ContractError as e:
        client.close()
        raise StartupError(f"{e}\n{FIX_HINT}") from None
    except GristError as e:
        client.close()
        raise StartupError(f"Could not check the Grist document: {e}") from None

    app = Flask(__name__)
    app.config["BESTYET"] = config
    app.extensions["bestyet_data"] = Data(client)
    errors.register(app, config)
    app.add_template_filter(marks.format_value, "mark")
    app.add_template_filter(dates.display, "day")

    @app.get("/")
    def home():
        data = current_data()
        athletes = data.athletes()
        wanted = request.args.get("athlete", type=int)
        selected = next((a for a in athletes if a.id == wanted), None)
        groups = selected and pbs.by_category(
            selected.id,
            data.personal_bests(),
            data.events(),
            data.results(),
            data.categories(),
        )
        return render_template(
            "home.html",
            athletes=athletes,
            selected=selected,
            groups=groups,
            today=dates.today(config.tz),
        )

    @app.get("/health")
    def health():
        return render_template("health.html")

    return app
