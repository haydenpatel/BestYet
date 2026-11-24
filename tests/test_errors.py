from dataclasses import replace

import httpx
import pytest
from app_fixtures import CONFIG
from fake_grist import KEY

from bestyet import create_app
from bestyet.data import current_data


def app_on(transport, config=CONFIG):
    """An app whose Grist answers through `transport`, with a route that reads Athletes."""
    app = create_app(config, verify=False, transport=transport)

    @app.get("/athletes")
    def athletes():
        return ",".join(a.name for a in current_data().athletes())

    return app


def assert_clean(response):
    text = response.get_data(as_text=True)
    assert "Traceback" not in text
    assert KEY not in text
    assert "Authorization" not in text and "Bearer" not in text
    assert "<!doctype html>" in text


def test_not_found_page(web):
    response = web.get("/nope")
    assert response.status_code == 404
    assert "That page does not exist." in response.text
    assert_clean(response)


def test_method_not_allowed_is_readable(web):
    response = web.post("/health")
    assert response.status_code == 405
    assert "Traceback" not in response.text


@pytest.mark.parametrize(
    "status, words",
    [
        (401, "rejected the API key"),
        (403, "denied access"),
        (404, "could not find something the app asked for"),
        (500, "server error"),
    ],
)
def test_grist_http_error_renders_a_readable_page(status, words):
    app = app_on(httpx.MockTransport(lambda r: httpx.Response(status, json={"error": "nope"})))
    response = app.test_client().get("/athletes")
    assert response.status_code == 502
    assert words in response.text
    assert_clean(response)


def test_unreachable_grist_names_only_the_host():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    response = app_on(httpx.MockTransport(refuse)).test_client().get("/athletes")
    assert response.status_code == 502
    assert "Could not reach Grist at grist.example.test" in response.text
    assert "/api/" not in response.text
    assert_clean(response)


def test_url_credentials_never_appear():
    url = "https://" + "demo-user:" + "demo-pass" + "@grist.example.test"
    config = replace(CONFIG, grist_url=url)
    app = app_on(httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "bad"})), config)
    response = app.test_client().get("/athletes")
    assert response.status_code == 502
    assert "demo-pass" not in response.text and "demo-user" not in response.text


def test_unexpected_error_hides_the_traceback(app):
    @app.get("/boom")
    def boom():
        raise RuntimeError("secret detail " + KEY)

    response = app.test_client().get("/boom")
    assert response.status_code == 500
    assert "Something went wrong" in response.text
    assert "secret detail" not in response.text
    assert_clean(response)
