import guard
import pytest

from bestyet.config import Config
from bestyet.grist import GristClient


def pytest_collection_modifyitems(config, items):
    """Integration tests are skipped unless BESTYET_INTEGRATION is set (never in CI)."""
    if guard.integration_enabled():
        return
    skip = pytest.mark.skip(reason=f"integration test: set {guard.ENABLE_VAR}=1 to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def test_grist():
    """A client for the configured document, only if its name contains TEST.

    Use for every test that writes, deletes or loads demo data. Fails (never skips) when the
    document is not a TEST document, so a misconfigured run cannot pass silently.
    """
    config = Config.from_env()
    try:
        guard.require_test_document(
            config.grist_url, config.grist_doc_id, config.grist_api_key.reveal()
        )
    except guard.GuardError as error:
        pytest.fail(str(error), pytrace=False)
    with GristClient(config.grist_url, config.grist_doc_id, config.grist_api_key) as client:
        yield client
