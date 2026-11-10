"""BestYet: a small mobile web app in front of a Grist document."""

from flask import Flask, render_template

from bestyet.config import Config


def create_app(config: Config | None = None) -> Flask:
    """Create the app. `/health` works without configuration."""
    app = Flask(__name__)
    app.config["BESTYET"] = config

    @app.get("/health")
    def health():
        return render_template("health.html")

    return app
