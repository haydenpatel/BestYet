import pytest

from bestyet import create_app


@pytest.fixture
def client():
    return create_app().test_client()


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert b"ok" in response.data
