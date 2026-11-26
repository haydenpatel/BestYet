import re

import httpx
from app_fixtures import CONFIG

from bestyet import create_app


def chips(response):
    """The athlete chips of a page: (name, href, is current)."""
    found = re.findall(r'<a class="chip" href="([^"]*)"([^>]*)>([^<]*)</a>', response.text)
    return [(name, href, 'aria-current="true"' in attrs) for href, attrs, name in found]


def test_home_lists_the_athletes_as_links(web):
    response = web.get("/")
    assert response.status_code == 200
    assert chips(response) == [
        ("Athlete A", "/?athlete=1", False),
        ("Athlete B", "/?athlete=2", False),
        ("Athlete C", "/?athlete=3", False),
    ]
    assert "Choose an athlete." in response.text


def test_the_chosen_athlete_is_marked(web):
    response = web.get("/?athlete=2")
    assert [current for _, _, current in chips(response)] == [False, True, False]
    assert "<h2>Athlete B</h2>" in response.text
    assert "Choose an athlete." not in response.text


def test_an_unknown_athlete_chooses_nobody(web):
    for query in ("99", "abc", ""):
        response = web.get(f"/?athlete={query}")
        assert response.status_code == 200
        assert not any(current for _, _, current in chips(response))
        assert "Choose an athlete." in response.text


def test_no_athletes(fake, web):
    fake.rows["Athletes"] = []
    response = web.get("/")
    assert "No athletes yet. Add them in Grist." in response.text
    assert "data-athlete-chips" not in response.text


def test_names_are_escaped(fake, web):
    fake.rows["Athletes"][0]["fields"]["Name"] = "<script>alert(1)</script>"
    response = web.get("/?athlete=1")
    assert "<script>alert" not in response.text
    assert "&lt;script&gt;" in response.text


def test_links_keep_a_path_prefix(web):
    response = web.get("/?athlete=1", environ_overrides={"SCRIPT_NAME": "/bestyet"})
    assert chips(response)[0][1] == "/bestyet/?athlete=1"
    assert 'href="/bestyet/static/app.css"' in response.text


def test_assets_are_local_and_served_by_the_app(web):
    html = web.get("/").text
    assets = re.findall(r'(?:<link [^>]*href|<script [^>]*src)="([^"]*)"', html)
    assert assets == ["/static/app.css", "/static/app.js"]
    for path in assets:
        with web.get(path) as response:
            assert response.status_code == 200
            for external in ("http:", "https:", "@import", "url("):
                assert external not in response.text


def test_without_scripting_every_athlete_is_a_link(fake, web):
    fake.rows["Athletes"] = [{"id": i, "fields": {"Name": f"Athlete {i}"}} for i in range(1, 31)]
    response = web.get("/")
    assert len(chips(response)) == 30
    assert "<select" not in response.text  # the dropdown is added by app.js only


def test_the_script_runs_after_the_chips_without_deferring(web):
    html = web.get("/").text
    assert '<script src="/static/app.js"></script>' in html
    assert html.index("data-athlete-chips") < html.index("<script")


def test_error_pages_share_the_layout_and_lead_home():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, json={"error": "no"}))
    response = create_app(CONFIG, verify=False, transport=transport).test_client().get("/")
    assert response.status_code == 502
    assert 'href="/static/app.css"' in response.text
    assert 'class="button" href="/"' in response.text
