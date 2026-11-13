import pytest
from fake_grist import BASE, DOC, KEY, FakeGrist
from test_init_grist import ENV, run

from bestyet import StartupError, create_app, schema
from bestyet.check import ContractError, check_document, require_contract
from bestyet.config import Config
from bestyet.grist import GristClient
from bestyet.init_grist import main

CONFIG = Config.from_env(ENV)


def initialised():
    """A document the init script has fully set up (through the fake)."""
    fake = FakeGrist(new_doc_tables={})
    run(fake, "--create", "--workspace", "Work")
    fake.tables.pop("Table1", None)
    return fake


def differences(fake):
    with GristClient(BASE, DOC, CONFIG.grist_api_key, transport=fake.transport) as client:
        return check_document(client)


def test_initialised_document_passes():
    assert differences(initialised()) == []


def test_missing_column_is_named():
    fake = initialised()
    del fake.tables["Results"]["Mark"]
    assert differences(fake) == ["Results: column 'Mark' is missing"]


def test_every_difference_is_reported():
    fake = initialised()
    del fake.tables["Results"]["Mark"]
    del fake.tables["Athletes"]["Grade"]
    del fake.tables["Meets"]
    fake.tables["Events"]["Kind"]["type"] = "Text"
    found = differences(fake)
    assert "Results: column 'Mark' is missing" in found
    assert "Athletes: column 'Grade' is missing" in found
    assert "table 'Meets' is missing" in found
    assert "Events.Kind has type Text, contract says Choice" in found
    assert len(found) == 4


def test_missing_table_does_not_also_report_its_columns():
    fake = initialised()
    del fake.tables["Implements"]
    assert differences(fake) == ["table 'Implements' is missing"]


def test_case_only_names_are_differences():
    fake = initialised()
    fake.tables["Results"]["mark"] = fake.tables["Results"].pop("Mark")
    fake.tables["athletes"] = fake.tables.pop("Athletes")
    assert differences(fake) == [
        "table 'athletes' exists, contract says 'Athletes'",
        "Results: column 'mark' exists, contract says 'Mark'",
    ]


def test_missing_summary_table_and_page_are_named():
    fake = initialised()
    del fake.tables[schema.SUMMARY.id]
    fake.summary_tables.clear()
    fake.views.clear()
    assert differences(fake) == [
        f"table {schema.SUMMARY.id!r} is missing",
        f"page {schema.SUMMARY.page!r} is missing",
    ]


def test_missing_page_alone_and_missing_best_column():
    fake = initialised()
    fake.views.clear()
    del fake.tables[schema.SUMMARY.id]["Best"]
    assert differences(fake) == [
        f"{schema.SUMMARY.id}: column 'Best' is missing",
        f"page {schema.SUMMARY.page!r} is missing",
    ]


def test_raw_data_view_is_not_a_page():
    fake = initialised()
    fake.views.clear()
    fake.views[1] = schema.SUMMARY.page
    orig = fake.doc

    def doc(method, rest, body):
        response = orig(method, rest, body)
        if rest == "tables/_grist_Views/records":
            records = [{"id": 1, "fields": {"name": schema.SUMMARY.page, "type": "raw_data"}}]
            return type(response)(200, json={"records": records})
        return response

    fake.doc = doc
    assert differences(fake) == [f"page {schema.SUMMARY.page!r} is missing"]


@pytest.mark.parametrize(
    "name", ["Personal bests", "personal bests", " PERSONAL BESTS ", "Personal Bests"]
)
def test_page_name_matches_ignoring_case_and_spaces(name, capsys):
    fake = initialised()
    fake.views.clear()
    fake.views[1] = name
    assert differences(fake) == []
    assert main(["--check"], ENV, fake.transport) == 0


def test_page_with_another_name_is_missing():
    fake = initialised()
    fake.views.clear()
    fake.views[1] = "Personal best"
    assert differences(fake) == [f"page {schema.SUMMARY.page!r} is missing"]


def test_plain_table_with_the_summary_name_is_not_a_summary_table():
    fake = initialised()
    fake.summary_tables.clear()
    assert differences(fake) == [f"table {schema.SUMMARY.id!r} is not a summary table of 'Results'"]


def test_formula_and_trigger_settings_are_checked():
    fake = initialised()
    results = fake.tables["Results"]
    results["Value"]["formula"] = ""
    results["IsPB"]["isFormula"] = False
    results["Grade"]["isFormula"] = True
    del results["Implement"]["recalcDeps"]
    results["Date"]["type"] = "Any"
    found = differences(fake)
    assert "Results.Value has no formula" in found
    assert "Results.IsPB should be a formula column" in found
    assert "Results.Grade should be a trigger-formula column" in found
    assert "Results.Implement should recalculate when Event changes" in found
    assert "Results.Date has type Any, contract says Date" in found


def test_trigger_with_wrong_dependency_is_reported():
    fake = initialised()
    fake.tables["Results"]["Grade"]["recalcDeps"] = ["L", fake.ref("Results", "Event")]
    assert differences(fake) == ["Results.Grade should recalculate when Athlete changes"]


def test_ref_columns_must_show_names():
    fake = initialised()
    del fake.tables["Results"]["Athlete"]["visibleCol"]
    del fake.tables[schema.SUMMARY.id]["Event"]["visibleCol"]
    assert differences(fake) == [
        "Results.Athlete should show Athletes.Name",
        f"{schema.SUMMARY.id}.Event should show Events.Name",
    ]


def test_unrelated_tables_and_extra_columns_are_ignored():
    fake = initialised()
    fake.tables["Notes"] = {"Text": {"type": "Text"}}
    fake.tables["Results"]["Extra"] = {"type": "Text"}
    assert differences(fake) == []


def test_check_only_reads():
    fake = initialised()
    fake.writes.clear()
    differences(fake)
    assert fake.writes == []


# CLI.


def test_cli_check_passes(capsys):
    fake = initialised()
    assert main(["--check"], ENV, fake.transport) == 0
    assert "matches the BestYet contract: no differences" in capsys.readouterr().out


def test_cli_check_lists_every_difference_and_fails(capsys):
    fake = initialised()
    del fake.tables["Results"]["Mark"]
    del fake.tables[schema.SUMMARY.id]
    fake.views.clear()
    fake.writes.clear()
    assert main(["--check"], ENV, fake.transport) == 1
    captured = capsys.readouterr()
    assert "3 differences" in captured.out
    assert "Running bestyet-init without --check adds what is missing" in captured.out
    assert "the document has 3 differences from the contract" in captured.err
    assert "Results: column 'Mark' is missing" in captured.out
    assert f"table {schema.SUMMARY.id!r} is missing" in captured.out
    assert f"page {schema.SUMMARY.page!r} is missing" in captured.out
    assert KEY not in captured.out + captured.err
    assert fake.writes == []


@pytest.mark.parametrize(
    "flag",
    [["--create", "--workspace", "Work"], ["--dry-run"], ["--yes"], ["--allow-existing-tables"]],
)
def test_cli_check_rejects_write_options(flag, capsys):
    with pytest.raises(SystemExit):
        main(["--check", *flag], ENV, initialised().transport)
    assert "read-only" in capsys.readouterr().err


def test_cli_check_needs_a_document_id(capsys):
    env = {k: v for k, v in ENV.items() if k != "GRIST_DOC_ID"}
    assert main(["--check"], env, initialised().transport) == 1
    assert "GRIST_DOC_ID" in capsys.readouterr().err


def test_cli_check_grist_failure_is_one_line(capsys):
    fake = initialised()
    fake.fail_on = "/tables"
    assert main(["--check"], ENV, fake.transport) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and KEY not in err


# App startup.


def test_require_contract_raises_with_all_differences():
    fake = initialised()
    del fake.tables["Results"]["Mark"]
    del fake.tables["Athletes"]
    client = GristClient(BASE, DOC, CONFIG.grist_api_key, transport=fake.transport)
    with client, pytest.raises(ContractError) as e:
        require_contract(client)
    assert len(e.value.differences) == 2
    assert "2 differences" in str(e.value)


def test_app_starts_on_a_matching_document():
    app = create_app(CONFIG, transport=initialised().transport)
    assert app.test_client().get("/health").status_code == 200


def test_app_refuses_to_start_on_a_mismatch():
    fake = initialised()
    del fake.tables["Results"]["Mark"]
    with pytest.raises(StartupError) as e:
        create_app(CONFIG, transport=fake.transport)
    assert "Results: column 'Mark' is missing" in e.value.format_message()
    assert KEY not in e.value.format_message()


def test_app_startup_reports_grist_failure_without_the_key():
    fake = initialised()
    fake.fail_on = "/tables"
    with pytest.raises(StartupError) as e:
        create_app(CONFIG, transport=fake.transport)
    assert "Could not check the Grist document" in e.value.format_message()
    assert KEY not in e.value.format_message()


def test_app_startup_loads_config_from_the_environment(monkeypatch):
    for k in ("GRIST_URL", "GRIST_DOC_ID", "GRIST_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir("/")  # no dev .env here
    with pytest.raises(StartupError) as e:
        create_app()
    assert "GRIST_URL is missing" in e.value.format_message()


def test_app_and_script_have_no_test_name_logic():
    import inspect

    import bestyet.check
    import bestyet.init_grist

    for module in (bestyet, bestyet.check, bestyet.init_grist):
        assert "TEST" not in inspect.getsource(module)
