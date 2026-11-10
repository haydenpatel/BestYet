"""BestYet: a small mobile web app in front of a Grist document."""

from flask import Flask, render_template


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return render_template("health.html")

    return app
