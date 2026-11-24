from datetime import date

import pytest
from fake_grist import BASE, DOC, KEY

from bestyet import schema
from bestyet.config import Secret
from bestyet.data import Data, current_data
from bestyet.grist import GristClient


@pytest.fixture
def data(app):
    with app.app_context():
        yield current_data()


def test_a_route_reads_demo_data_with_no_network(app):
    @app.get("/names")
    def names():
        return ",".join(a.name for a in current_data().athletes())

    assert app.test_client().get("/names").text == "Athlete A,Athlete B,Athlete C"


def test_athletes(data):
    assert [(a.name, a.grade, a.division) for a in data.athletes()] == [
        ("Athlete A", "Grade 8", "Girls"),
        ("Athlete B", "Grade 9", "Boys"),
        ("Athlete C", "Grade 9", "Girls"),
    ]


def test_meets_have_dates(data):
    assert [(m.name, m.date, m.location) for m in data.meets()] == [
        ("Demo Meet 1", date(2026, 3, 7), "Demo Field"),
        ("Demo Meet 2", date(2026, 4, 18), "Demo Stadium"),
    ]


def test_events_without_a_category_column(data):
    events = data.events()
    assert len(events) == len(schema.EXAMPLE_EVENTS)
    assert [(e.name, e.kind) for e in events[:1]] == [schema.EXAMPLE_EVENTS[0]]
    assert {e.category for e in events} == {None}


def test_events_with_a_category_column(app, fake, data):
    fake.tables["Events"]["Category"] = {"type": "Choice"}
    fake.rows["Events"][0]["fields"]["Category"] = "Sprints"
    events = data.events()
    assert events[0].category == "Sprints"
    assert events[1].category is None  # an empty cell


def test_results_resolve_names_and_keep_grist_computed_cells(data):
    results = data.results()
    assert len(results) == len(schema.DEMO_RESULTS)
    first = results[0]
    assert (first.athlete, first.event, first.meet) == ("Athlete A", "100m", "Demo Meet 1")
    assert (first.mark, first.place, first.value) == ("13.45", 2, 13.45)
    assert (first.grade, first.division, first.date) == ("Grade 8", "Girls", date(2026, 3, 7))
    assert first.is_pb is False
    assert results[1].is_pb is True  # 13.20 beats 13.45
    discus = next(r for r in results if r.event == "Discus" and r.is_pb)
    assert (discus.athlete, discus.implement, discus.value) == ("Athlete B", "500g", 23.10)


def test_results_tolerate_empty_and_error_cells(fake, data):
    fake.rows["Results"].append(
        {
            "id": 99,
            "fields": {
                "Athlete": 0,  # an empty Ref
                "Event": 999,  # a Ref to a row that is gone
                "Meet": ["E", "ValueError"],  # a Grist error cell
                "Mark": "",
                "Value": ["E", "ValueError"],
                "Date": None,
                "IsPB": None,
            },
        }
    )
    last = data.results()[-1]
    assert (last.athlete, last.athlete_id, last.event, last.meet, last.meet_id) == (
        None,
        None,
        None,
        None,
        None,
    )
    assert (last.mark, last.value, last.date, last.is_pb) == (None, None, None, False)


def test_personal_bests_come_from_the_summary_table(data):
    bests = {(p.athlete, p.event, p.implement): p for p in data.personal_bests()}
    assert len(bests) == 5
    sprint = bests[("Athlete A", "100m", None)]
    assert (sprint.count, sprint.best) == (2, 13.20)
    assert bests[("Athlete B", "Discus", "500g")].best == 23.10


def test_the_layer_never_writes(fake, data):
    data.athletes(), data.events(), data.meets(), data.results(), data.personal_bests()
    assert fake.writes == []


def test_data_can_be_built_from_a_client_alone(fake):
    client = GristClient(BASE, DOC, Secret(KEY), transport=fake.transport)
    assert len(Data(client).athletes()) == 3


def test_names_are_current_when_a_row_is_renamed(fake, data):
    assert data.results()[0].athlete == "Athlete A"
    fake.rows["Athletes"][0]["fields"]["Name"] = "Athlete Z"
    assert data.results()[0].athlete == "Athlete Z"
    assert {p.athlete for p in data.personal_bests()} >= {"Athlete Z"}
