import base64
import hashlib
import json
import re

import httpx
from app_fixtures import CONFIG

from bestyet import create_app
from bestyet.grist import DEFAULT_TIMEOUT


def chips(response):
    """The athlete chips of a page: (name, href, is current)."""
    found = re.findall(
        r'<a class="chip" id="athlete-\d+" href="([^"]*)"([^>]*)>([^<]*)</a>', response.text
    )
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


def test_the_title_names_the_chosen_athlete(web):
    # app.js announces the title after a swap, so it differs from athlete to athlete.
    assert "<title>Athlete B - Personal Bests - BestYet</title>" in web.get("/?athlete=2").text
    assert "<title>Personal Bests - BestYet</title>" in web.get("/").text


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
    assert "<title>&lt;script&gt;alert(1)&lt;/script&gt; - Personal Bests" in response.text


def test_links_keep_a_path_prefix(web):
    response = web.get("/?athlete=1", environ_overrides={"SCRIPT_NAME": "/bestyet"})
    assert chips(response)[0][1] == "/bestyet/?athlete=1"
    assert 'href="/bestyet/static/app.css"' in response.text


def test_assets_are_local_and_served_by_the_app(web):
    html = web.get("/").text
    assets = re.findall(r'(?:<link [^>]*href|<script [^>]*src)="([^"]*)"', html)
    assert assets == ["/static/app.css", "/static/htmx.min.js", "/static/app.js"]
    texts = [html]
    for path in assets:
        with web.get(path) as response:
            assert response.status_code == 200
            if path != "/static/htmx.min.js":  # as released; its checksum is tested
                texts.append(response.text)
    for text in texts:
        for external in ("http:", "https:", "@import", "url("):
            assert external not in text


def test_htmx_is_the_released_file(web):
    # htmx 2.0.11's dist/htmx.min.js: the integrity value in the htmx documentation.
    with web.get("/static/htmx.min.js") as response:
        digest = base64.b64encode(hashlib.sha384(response.data).digest()).decode()
    assert digest == "2OatzQy1H+Zd/IIrjr1TcuDGqLXeHhbooAyJY1KdQMKnr4LZ22k31GBLdYKHmVjg"
    with web.get("/static/htmx.LICENSE") as response:
        assert response.text.startswith("Zero-Clause BSD")


def htmx_config(web):
    return json.loads(re.search(r"name=\"htmx-config\" content='([^']*)'", web.get("/").text)[1])


def test_htmx_is_set_up_safely_and_keeps_no_pages(web):
    config = htmx_config(web)
    assert config["allowEval"] is False
    assert config["selfRequestsOnly"] is True
    assert (config["historyCacheSize"], config["refreshOnHistoryMiss"]) == (0, True)


def test_htmx_waits_longer_than_grist_and_shows_error_pages(web):
    config = htmx_config(web)
    assert config["timeout"] > DEFAULT_TIMEOUT * 1000
    assert {"code": "[45]..", "swap": True, "error": True} in config["responseHandling"]


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


def test_a_tapped_reload_is_dimmed_but_not_disabled(web):
    with web.get("/static/app.css") as response:
        css = response.text
    assert re.findall(r"\.reloading \{([^}]*)\}", css) == [" opacity: 0.4; "]
    with web.get("/static/app.js") as response:
        script = response.text
    assert 'on ? "Reloading…" : "Reload"' in script
    # restored whenever the message is cleared or shown, which includes a restored page
    assert "showReloading(link, false);" in script
    assert 'done();\n    warn("");\n    resetDropdown();' in script
    # the page is left only after a frame, or after a moment if none is drawn
    assert "event.preventDefault();\n    if (leaving) return;" in script
    assert "requestAnimationFrame(function () {\n        setTimeout(go, 50);" in script
    assert "setTimeout(go, 250);" in script


def test_every_page_can_be_swapped_in_and_has_the_feedback(web):
    for path in ("/", "/?athlete=1", "/nope"):
        html = web.get(path).text
        assert 'hx-boost="true" hx-target="#page" hx-select="#page"' in html
        assert html.count('id="page"') == 1
        # these stay while pages are swapped
        before = html[: html.index('id="page"')]
        assert 'role="status" data-loading-status></p>' in before
        assert 'role="alert" data-load-failed hidden>' in before
        # Reload retries with a full page load, not one by htmx
        assert '<a class="button" href="/" hx-boost="false">Reload</a>' in before
        assert '<div class="progress" aria-hidden="true"></div>' in before


def test_error_pages_share_the_layout_and_lead_home():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, json={"error": "no"}))
    response = create_app(CONFIG, verify=False, transport=transport).test_client().get("/")
    assert response.status_code == 502
    assert 'href="/static/app.css"' in response.text
    assert 'class="button" href="/">Go to Personal Bests</a>' in response.text
