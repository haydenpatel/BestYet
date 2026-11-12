import pytest
from fake_grist import BASE, DOC, KEY

from bestyet import create_app
from bestyet.config import Config


@pytest.fixture
def client():
    config = Config.from_env({"GRIST_URL": BASE, "GRIST_DOC_ID": DOC, "GRIST_API_KEY": KEY})
    return create_app(config, verify=False).test_client()


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert b"ok" in response.data
