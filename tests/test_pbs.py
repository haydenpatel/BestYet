import pytest

from bestyet import pbs
from bestyet.data import current_data


@pytest.fixture
def grouped(app):
    """Call for an athlete's groups as {category: {(event, implement, best, new)}}."""

    def read(athlete_id):
        with app.app_context():
            data = current_data()
            groups = pbs.by_category(
                athlete_id, data.personal_bests(), data.events(), data.results()
            )
        return {
            category: {(r.pb.event, r.pb.implement, r.pb.best, r.is_new) for r in rows}
            for category, rows in groups.items()
        }

    return read


def test_only_the_athletes_rows_by_category(grouped):
    assert grouped(1) == {
        "Sprints": {("100m", None, 13.20, True)},
        "Middle distance": {("800m", None, 135.30, True)},
    }
    assert grouped(2) == {"Throws": {("Discus", "500g", 23.10, True)}}


def test_a_tie_of_an_earlier_best_is_not_new(grouped):
    assert grouped(3)["Jumps"] == {("Long Jump", None, 4.52, False)}


def test_a_blank_category_is_other(fake, grouped):
    for event in fake.rows["Events"]:
        if event["fields"]["Name"] == "100m":
            event["fields"]["Category"] = ""
    assert grouped(1)[pbs.OTHER] == {("100m", None, 13.20, True)}


def test_new_is_per_implement(fake, grouped):
    summary = fake.rows["Results_summary_Athlete_Event_Implement"]
    discus = next(r["fields"] for r in summary if r["fields"]["Implement"] == "500g")
    summary.append({"id": 99, "fields": {**discus, "Implement": "750g", "Best": 19.0}})
    assert grouped(2)["Throws"] == {
        ("Discus", "500g", 23.10, True),
        ("Discus", "750g", 19.0, False),
    }


def test_an_athlete_with_no_results_has_no_groups(grouped):
    assert grouped(99) == {}
