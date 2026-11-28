from datetime import date

import pytest

from bestyet import dates, pbs, schema
from bestyet.data import Event, PersonalBest, current_data


@pytest.fixture
def grouped(app):
    """Call for an athlete's groups as {category: [(event, implement, best, new)]}."""

    def read(athlete_id):
        with app.app_context():
            data = current_data()
            groups = pbs.by_category(
                athlete_id,
                data.personal_bests(),
                data.events(),
                data.results(),
                data.categories(),
            )
        return {
            category: [(r.pb.event, r.pb.implement, r.pb.best, r.is_new) for r in rows]
            for category, rows in groups.items()
        }

    return read


def summary_rows(fake):
    return fake.rows[schema.SUMMARY.id]


def best(event_id, name, implement=None, when=None, value=1.0):
    return PersonalBest(
        1, "Athlete A", event_id, name, implement, 1, value, when, None, None, repr(implement)
    )


def arranged(rows, categories, choices=()):
    events = [Event(i, "", "Time", c) for i, c in categories.items()]
    groups = pbs.by_category(1, rows, events, [], choices)
    return {c: [(r.pb.event, r.pb.implement) for r in rs] for c, rs in groups.items()}


def test_only_the_athletes_rows_by_category(grouped):
    assert grouped(1) == {
        "Sprints": [("100m", None, 13.20, True)],
        "Middle distance": [("800m", None, 135.30, True)],
    }
    assert grouped(2) == {"Throws": [("Discus", "500g", 23.10, True)]}


def test_a_tie_of_an_earlier_best_is_not_new(grouped):
    assert grouped(3)["Jumps"] == [("Long Jump", None, 4.52, False)]


def test_a_blank_category_is_other(fake, grouped):
    for event in fake.rows["Events"]:
        if event["fields"]["Name"] == "100m":
            event["fields"]["Category"] = ""
    assert grouped(1)[pbs.OTHER] == [("100m", None, 13.20, True)]


def test_new_is_per_implement(fake, grouped):
    discus = next(r["fields"] for r in summary_rows(fake) if r["fields"]["Implement"] == "500g")
    older = {**discus, "Implement": "750g", "Best": 19.0, "BestDate": discus["BestDate"] - 86400}
    summary_rows(fake).append({"id": 99, "fields": older})
    assert grouped(2)["Throws"] == [
        ("Discus", "500g", 23.10, True),
        ("Discus", "750g", 19.0, False),
    ]


def test_implements_differing_only_by_spaces_are_not_both_new(fake, grouped):
    discus = next(r["fields"] for r in summary_rows(fake) if r["fields"]["Implement"] == "500g")
    summary_rows(fake).append({"id": 99, "fields": {**discus, "Implement": "500g ", "Best": 19.0}})
    assert sorted(grouped(2)["Throws"], key=lambda row: row[2]) == [
        ("Discus", "500g", 19.0, False),
        ("Discus", "500g", 23.10, True),
    ]


@pytest.mark.parametrize("options", ["not json", "[1]", '{"choices": 5}', "", None])
def test_unreadable_category_choices_leave_the_categories_by_name(fake, app, web, options):
    fake.tables["Events"]["Category"]["widgetOptions"] = options
    with app.app_context():
        assert current_data().categories() == []
    html = web.get("/?athlete=1").text
    assert html.index(">Middle distance</h3>") < html.index(">Sprints</h3>")


def test_an_athlete_with_no_results_has_no_groups(grouped):
    assert grouped(99) == {}


def test_categories_follow_grists_choices_with_other_last(app):
    with app.app_context():
        assert current_data().categories() == list(schema.EVENT_CATEGORIES)
    rows = [best(i, f"Event {i}") for i in range(1, 6)]
    categories = {1: None, 2: "Walks", 3: "Throws", 4: "Jumps", 5: "Relays"}
    assert list(arranged(rows, categories, ["Throws", "Other", "Jumps"])) == [
        "Throws",
        "Jumps",
        "Relays",  # not a choice: by name, after the choices
        "Walks",
        "Other",
    ]


def test_events_by_the_number_in_the_name_then_by_name():
    names = ["Walk", "200m", "high jump", "100m Hurdles", "80m", "100m", "Long Jump"]
    rows = [best(i, n) for i, n in enumerate(names, 1)]
    got = arranged(rows, dict.fromkeys(range(1, 8), "Sprints"))["Sprints"]
    assert [event for event, _ in got] == [
        "80m",
        "100m",
        "100m Hurdles",
        "200m",
        "high jump",
        "Long Jump",
        "Walk",
    ]


def test_implements_are_newest_best_first_and_undated_last():
    rows = [
        best(1, "Discus", "500g", date(2025, 11, 8)),
        best(1, "Discus", "1kg"),
        best(1, "Discus", "750g", date(2026, 4, 18)),
    ]
    got = arranged(rows, {1: "Throws"})["Throws"]
    assert [implement for _, implement in got] == ["750g", "500g", "1kg"]


def test_rows_with_no_event_or_no_best_are_left_out():
    rows = [best(None, None), best(1, "100m", value=None)]
    assert arranged(rows, {1: "Sprints"}) == {}


def test_the_page_shows_a_card_per_category(web):
    html = " ".join(web.get("/?athlete=1").text.split())
    assert "<h2>Athlete A</h2>" in html
    assert html.index('<h3 class="category">Sprints</h3>') < html.index(">Middle distance</h3>")
    assert html.count('<ul class="card">') == 2
    assert '<span class="pb-event">100m</span>' in html
    # the badge comes just before the mark
    assert '<span class="badge">New PB</span> <span class="pb-mark">13.20 s</span>' in html
    assert '<span class="pb-mark">2:15.30</span>' in html
    assert "Demo Meet 2</div>" in html
    assert "No personal bests yet." not in html


def test_the_page_shows_implement_date_and_meet(web, monkeypatch):
    monkeypatch.setattr(dates, "today", lambda tz: date(2026, 11, 28))
    html = web.get("/?athlete=2").text
    assert "Discus · 500g</span>" in html
    assert '<span class="pb-mark">23.10 m</span>' in html
    assert '<div class="pb-when">18 Apr · Demo Meet 2</div>' in html
    monkeypatch.setattr(dates, "today", lambda tz: date(2027, 1, 1))
    assert "18 Apr 2026 · Demo Meet 2" in web.get("/?athlete=2").text


@pytest.mark.parametrize(
    "missing, line",
    [
        ({"BestDate": None}, "Demo Meet 2"),
        ({"BestMeet": 0}, "18 Apr"),
        ({"BestDate": None, "BestMeet": 0}, None),
    ],
)
def test_a_missing_date_or_meet_is_omitted(fake, web, monkeypatch, missing, line):
    monkeypatch.setattr(dates, "today", lambda tz: date(2026, 11, 28))
    for row in summary_rows(fake):
        row["fields"].update(missing)
    html = web.get("/?athlete=2").text
    if line:
        assert f'<div class="pb-when">{line}</div>' in html
    else:
        assert "pb-when" not in html


def test_the_page_with_no_personal_bests(fake, web):
    fake.rows[schema.SUMMARY.id] = []
    html = web.get("/?athlete=1").text
    assert '<p class="note">No personal bests yet.</p>' in html
    assert 'class="card"' not in html


def test_nothing_is_read_for_personal_bests_until_an_athlete_is_chosen(fake, web):
    del fake.tables[schema.SUMMARY.id]
    assert web.get("/").status_code == 200
