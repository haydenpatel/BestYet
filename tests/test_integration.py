"""Integration test against a real Grist TEST document (skipped unless BESTYET_INTEGRATION=1).

Adds one throwaway meet and one throwaway result, checks the computed columns, then deletes
exactly the rows it added. It relies on the demo data (Athletes, Events, Implements) being
loaded but not on any particular document name beyond the TEST guard in `test_grist`.
"""

import uuid

import pytest

pytestmark = pytest.mark.integration

MEET_DATE = 946684800  # 2000-01-01 (Grist stores dates as epoch seconds)
UNBEATABLE_MARK = "9999.99"  # a Distance mark no existing result can beat, so IsPB is True


def _find_setup(grist):
    """An athlete with a Grade and a Distance event that has an Implement for that Grade."""
    events = {e["id"]: e["fields"] for e in grist.records("Events")}
    athletes = [a for a in grist.records("Athletes") if a["fields"].get("Grade")]
    for implement in grist.records("Implements"):
        f = implement["fields"]
        event = events.get(f.get("Event"))
        if not event or event.get("Kind") != "Distance" or not f.get("Weight"):
            continue
        for athlete in athletes:
            if athlete["fields"]["Grade"] == f.get("Grade"):
                return athlete, f["Event"], f
    pytest.skip("the document has no athlete with a matching Implements row (load demo data)")


def test_result_computed_columns(test_grist):
    grist = test_grist
    athlete, event_id, implement = _find_setup(grist)
    meets_before = len(grist.records("Meets"))
    results_before = len(grist.records("Results"))
    meet_ids: list[int] = []
    result_ids: list[int] = []
    try:
        meet_ids = grist.add(
            "Meets",
            [{"Name": f"TEST throwaway {uuid.uuid4().hex[:8]}", "Date": MEET_DATE}],
        )
        result_ids = grist.add(
            "Results",
            [
                {
                    "Athlete": athlete["id"],
                    "Event": event_id,
                    "Meet": meet_ids[0],
                    "Mark": UNBEATABLE_MARK,
                }
            ],
        )
        (row,) = [r for r in grist.records("Results") if r["id"] == result_ids[0]]
        fields = row["fields"]
        assert fields["Value"] == float(UNBEATABLE_MARK)
        assert fields["Grade"] == athlete["fields"]["Grade"]
        assert fields["Implement"] == implement["Weight"]
        assert fields["IsPB"] is True
        assert fields["Date"] == MEET_DATE
    finally:
        try:
            if result_ids:
                grist.delete("Results", result_ids)
        finally:
            # Still try to remove the meet if the result delete failed.
            if meet_ids:
                grist.delete("Meets", meet_ids)
    assert len(grist.records("Results")) == results_before
    assert len(grist.records("Meets")) == meets_before
