import pytest
from fastapi.testclient import TestClient

from minutemind.api import TOKEN_HEADER, create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app("test-token"))


def test_health_with_token(client: TestClient) -> None:
    res = client.get("/health", headers={TOKEN_HEADER: "test-token"})
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_rejects_missing_token(client: TestClient) -> None:
    assert client.get("/health").status_code == 401


def test_rejects_wrong_token(client: TestClient) -> None:
    assert client.get("/health", headers={TOKEN_HEADER: "nope"}).status_code == 401


def test_empty_token_not_allowed() -> None:
    with pytest.raises(ValueError):
        create_app("")
