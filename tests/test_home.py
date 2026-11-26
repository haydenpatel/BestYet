import re

import httpx
from app_fixtures import CONFIG

from bestyet import create_app
from bestyet.grist import DEFAULT_TIMEOUT


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
    assert assets == ["/static/app.css", "/static/loading.js"]
    texts = [html]
    for path in assets:
        with web.get(path) as response:
            assert response.status_code == 200
            texts.append(response.text)
    for text in texts:
        for external in ("http:", "https:", "@import", "url("):
            assert external not in text


def test_without_scripting_every_athlete_is_a_link(fake, web):
    fake.rows["Athletes"] = [{"id": i, "fields": {"Name": f"Athlete {i}"}} for i in range(1, 31)]
    response = web.get("/")
    assert len(chips(response)) == 30
    assert "<select" not in response.text  # the dropdown is added by the script only


def test_the_script_is_inline_right_after_the_chips(web):
    # An external or later script would let the chips be painted before they become a dropdown.
    html = web.get("/").text
    assert re.search(r"</nav>\s*</div>\s*</div>\s*<script>\s*//", html)
    assert html.count("<script>") == 1
    assert "document.currentScript.previousElementSibling" in html


def test_held_chips_always_come_back(web):
    # Hidden only where scripting is on, and only for as long as the animation runs.
    with web.get("/static/app.css") as response:
        css = response.text
    assert "@media (scripting: enabled) {\n  .switcher:not(.ready) { animation: held 1s" in css
    assert "@keyframes held { from { visibility: hidden; } }" in css


def test_every_page_has_the_loading_feedback(web):
    for path in ("/", "/nope"):
        html = web.get(path).text
        # in the head and not deferred, so that it also covers a page that redirects itself
        assert '<script src="/static/loading.js"></script>\n</head>' in html
        assert 'role="status" data-loading-status></p>' in html
        assert '<div class="progress" aria-hidden="true">' in html


def test_loading_feedback_gives_up_after_the_grist_timeout(web):
    with web.get("/static/loading.js") as response:
        give_up = int(re.search(r"GIVE_UP_AFTER = (\d+);", response.text)[1])
    assert give_up > DEFAULT_TIMEOUT * 1000


def test_error_pages_share_the_layout_and_lead_home():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, json={"error": "no"}))
    response = create_app(CONFIG, verify=False, transport=transport).test_client().get("/")
    assert response.status_code == 502
    assert 'href="/static/app.css"' in response.text
    assert 'class="button" href="/"' in response.text
