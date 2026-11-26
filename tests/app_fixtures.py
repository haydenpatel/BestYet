"""Fixtures for route tests: the app on a fake Grist seeded with the init script's demo data.

Loaded for every test through `pytest_plugins` in conftest.py. The fake does not run Grist's
formulas, so the computed cells (Value, Date, Grade, Division, Implement, IsPB, IsNewPB, the meets'
ResultCount and the summary table) are filled in here, once, in the shape Grist returns them.
"""

import pytest
from fake_grist import BASE, DOC, KEY
from init_helpers import full_fake

from bestyet import create_app, dates, marks, schema
from bestyet.config import Config

CONFIG = Config.from_env({"GRIST_URL": BASE, "GRIST_DOC_ID": DOC, "GRIST_API_KEY": KEY})


def seed_demo(fake):
    """Add the demo athletes, meets, results and personal bests to a `full_fake` document."""
    ids = {}  # (table, name) -> row id
    for table, rows in (
        ("Athletes", [{"Name": n, "Grade": g, "Division": d} for n, g, d in schema.DEMO_ATHLETES]),
        (
            "Meets",
            [
                {"Name": n, "Date": dates.to_grist(dates.parse_iso(d)), "Location": loc}
                for n, d, loc in schema.DEMO_MEETS
            ],
        ),
    ):
        fake.rows[table] = [{"id": i, "fields": f} for i, f in enumerate(rows, 1)]
        ids.update({(table, f["Name"]): i for i, f in enumerate(rows, 1)})
    for r in fake.rows["Events"]:
        ids[("Events", r["fields"]["Name"])] = r["id"]
    kinds = {r["id"]: r["fields"]["Kind"] for r in fake.rows["Events"]}
    athletes = {r["id"]: r["fields"] for r in fake.rows["Athletes"]}
    meets = {r["id"]: r["fields"] for r in fake.rows["Meets"]}

    results = []
    for i, (athlete, event, meet, mark, place) in enumerate(schema.DEMO_RESULTS, 1):
        a, e, m = ids[("Athletes", athlete)], ids[("Events", event)], ids[("Meets", meet)]
        implement = "500g" if event == "Discus" else None
        results.append(
            {
                "Athlete": a,
                "Event": e,
                "Meet": m,
                "Mark": mark,
                "Place": place,
                "Grade": athletes[a]["Grade"],
                "Division": athletes[a]["Division"],
                "Implement": implement,
                "Value": marks.parse_mark(mark).value,
                "Date": meets[m]["Date"],
            }
        )
    best = {}
    for r in results:
        key = (r["Athlete"], r["Event"], r["Implement"])
        pick = min if kinds[r["Event"]] == "Time" else max
        best[key] = pick(best.get(key, r["Value"]), r["Value"])
    for r in results:
        r["IsPB"] = r["Value"] == best[(r["Athlete"], r["Event"], r["Implement"])]
    first = {}  # the earliest result with the best value, per key
    for r in sorted(results, key=lambda r: r["Date"]):
        key = (r["Athlete"], r["Event"], r["Implement"])
        if r["Value"] == best[key]:
            first.setdefault(key, r)
    latest = max(r["Date"] for r in results)
    for r in results:
        key = (r["Athlete"], r["Event"], r["Implement"])
        r["IsNewPB"] = first.get(key) is r and r["Date"] == latest
    for i, m in enumerate(fake.rows["Meets"], 1):
        m["fields"]["ResultCount"] = sum(r["Meet"] == i for r in results)
    fake.rows["Results"] = [{"id": i, "fields": f} for i, f in enumerate(results, 1)]
    fake.rows[schema.SUMMARY.id] = [
        {
            "id": i,
            "fields": {
                "Athlete": a,
                "Event": e,
                "Implement": imp,
                "count": sum((r["Athlete"], r["Event"], r["Implement"]) == k for r in results),
                "Best": v,
                "BestMeet": first[k]["Meet"],
                "BestDate": first[k]["Date"],
            },
        }
        for i, (k, v) in enumerate(best.items(), 1)
        for a, e, imp in [k]
    ]
    return fake


@pytest.fixture
def fake():
    """A fully initialised fake Grist document holding the demo data."""
    return seed_demo(full_fake())


@pytest.fixture
def app(fake):
    return create_app(CONFIG, verify=False, transport=fake.transport)


@pytest.fixture
def web(app):
    """A Flask test client for the app on the seeded fake Grist, with no network."""
    return app.test_client()
