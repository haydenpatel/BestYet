import pytest

from bestyet.lookups import LookupFailed, Lookups


class FakeClient:
    def __init__(self, tables):
        self.tables = tables
        self.calls = []

    def records(self, table):
        self.calls.append(table)
        return self.tables[table]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def rec(row_id, name):
    return {"id": row_id, "fields": {"Name": name}}


@pytest.fixture
def client():
    return FakeClient(
        {
            "Athletes": [
                rec(1, "Athlete A"),
                rec(2, "Athlete B"),
                rec(6, " Padded "),
                rec(3, "Twin"),
                rec(4, "Twin"),
            ],
            "Events": [rec(10, "100m"), rec(11, "Long jump"), {"id": 12, "fields": {"Name": None}}],
            "Meets": [rec(20, "Club night 1")],
        }
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def lookups(client, clock):
    return Lookups(client, clock=clock)


def test_resolves_each_table(lookups):
    assert lookups.athlete_id("Athlete B") == 2
    assert lookups.event_id("Long jump") == 11
    assert lookups.meet_id("Club night 1") == 20


def test_reverse_lookup(lookups):
    assert lookups.athlete_name(1) == "Athlete A"
    assert lookups.event_name(10) == "100m"
    assert lookups.meet_name(20) == "Club night 1"
    assert lookups.athlete_name(6) == "Padded"
    assert lookups.athlete_name(999) is None
    assert lookups.event_name(12) is None


def test_trims_whitespace_and_is_case_sensitive(lookups):
    assert lookups.athlete_id("  Athlete A \n") == 1
    with pytest.raises(LookupFailed):
        lookups.athlete_id("athlete a")


def test_unknown_name(lookups):
    with pytest.raises(LookupFailed, match="No athlete named 'Nobody'"):
        lookups.athlete_id("Nobody")
    with pytest.raises(LookupFailed, match="No meet named"):
        lookups.meet_id("")


def test_duplicate_name_is_ambiguous(lookups):
    with pytest.raises(LookupFailed, match="More than one athlete is named 'Twin'"):
        lookups.athlete_id("Twin")


def test_failure_is_a_lookup_error(lookups):
    with pytest.raises(LookupError):
        lookups.event_id("Nope")


def test_cache_within_ttl(lookups, client, clock):
    lookups.athlete_id("Athlete A")
    lookups.athlete_name(2)
    clock.now += 29
    lookups.athlete_id("Athlete A")
    assert client.calls == ["Athletes"]
    clock.now += 2
    lookups.athlete_id("Athlete A")
    assert client.calls == ["Athletes", "Athletes"]


def test_new_name_resolves_without_waiting_for_ttl(lookups, client):
    lookups.athlete_id("Athlete A")
    client.tables["Athletes"].append(rec(5, "Athlete C"))
    assert lookups.athlete_id("Athlete C") == 5
    assert client.calls == ["Athletes", "Athletes"]


def test_unknown_name_reloads_exactly_once(lookups, client):
    lookups.athlete_id("Athlete A")
    with pytest.raises(LookupFailed):
        lookups.athlete_id("Nobody")
    assert client.calls == ["Athletes", "Athletes"]
    # A cold cache loads once, with no extra reload.
    lookups.invalidate()
    client.calls.clear()
    with pytest.raises(LookupFailed):
        lookups.athlete_id("Nobody")
    assert client.calls == ["Athletes"]


def test_tables_cached_separately(lookups, client):
    lookups.athlete_id("Athlete A")
    lookups.event_id("100m")
    assert client.calls == ["Athletes", "Events"]


def test_invalidate_forces_reload(lookups, client):
    lookups.meet_id("Club night 1")
    client.tables["Meets"].append(rec(21, "Club night 2"))
    lookups.invalidate()
    assert lookups.meet_id("Club night 2") == 21
    assert client.calls == ["Meets", "Meets"]
