from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from minutemind import store
from minutemind.api import TOKEN_HEADER, create_app
from minutemind.session import RecordingSession

from conftest import LOOPBACK, MIC, FakeRecorder, fake_devices

AUTH = {TOKEN_HEADER: "test-token"}


@pytest.fixture
def client(db: store.Store) -> Iterator[TestClient]:
    session = RecordingSession(db, recorder_factory=FakeRecorder, device_lister=fake_devices)
    with TestClient(create_app("test-token", db=db, session=session)) as c:
        yield c


def test_health_with_token(client: TestClient) -> None:
    res = client.get("/health", headers=AUTH)
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_token_as_query_parameter(client: TestClient) -> None:
    assert client.get("/health?token=test-token").status_code == 200


def test_rejects_missing_or_wrong_token(client: TestClient) -> None:
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={TOKEN_HEADER: "nope"}).status_code == 401
    assert client.get("/health?token=nope").status_code == 401


def test_empty_token_not_allowed() -> None:
    with pytest.raises(ValueError):
        create_app("")


def test_record_then_play_back(client: TestClient) -> None:
    start = client.post(
        "/recording/start",
        json={"title": "Demo", "mic_device_id": MIC.id, "system_device_id": LOOPBACK.id},
        headers=AUTH,
    )
    assert start.status_code == 200
    meeting_id = start.json()["id"]

    status = client.get("/recording", headers=AUTH).json()
    assert status["state"] == "recording"
    assert status["meeting"]["id"] == meeting_id
    # Audio isn't served while the file is still being written.
    assert client.get(f"/meetings/{meeting_id}/audio/mix", headers=AUTH).status_code == 409

    assert client.post("/recording/pause", headers=AUTH).json()["state"] == "paused"
    assert client.post("/recording/resume", headers=AUTH).json()["state"] == "recording"
    stopped = client.post("/recording/stop", headers=AUTH).json()
    assert stopped["status"] == "completed"

    listed = client.get("/meetings", headers=AUTH).json()
    assert [m["id"] for m in listed] == [meeting_id]

    for track in ("mix", "mic", "system"):
        res = client.get(f"/meetings/{meeting_id}/audio/{track}?token=test-token")
        assert res.status_code == 200
        assert res.headers["content-type"] == "audio/wav"
        assert res.content[:4] == b"RIFF"

    ranged = client.get(
        f"/meetings/{meeting_id}/audio/mix", headers={**AUTH, "Range": "bytes=0-99"}
    )
    assert ranged.status_code == 206  # seeking in the player needs range requests
    assert len(ranged.content) == 100


def test_conflicts_return_409(client: TestClient) -> None:
    assert client.post("/recording/stop", headers=AUTH).status_code == 409
    body = {"mic_device_id": MIC.id}
    assert client.post("/recording/start", json=body, headers=AUTH).status_code == 200
    assert client.post("/recording/start", json=body, headers=AUTH).status_code == 409


def test_rename_and_delete(client: TestClient) -> None:
    client.post("/recording/start", json={"mic_device_id": MIC.id}, headers=AUTH)
    meeting_id = client.post("/recording/stop", headers=AUTH).json()["id"]

    res = client.patch(f"/meetings/{meeting_id}", json={"title": "  Renamed "}, headers=AUTH)
    assert res.json()["title"] == "Renamed"
    assert client.patch(f"/meetings/{meeting_id}", json={"title": ""}, headers=AUTH).status_code == 422

    assert client.get(f"/meetings/{meeting_id}/audio/system", headers=AUTH).status_code == 404
    assert client.get(f"/meetings/{meeting_id}/audio/bogus", headers=AUTH).status_code == 404

    assert client.delete(f"/meetings/{meeting_id}", headers=AUTH).status_code == 204
    assert client.get(f"/meetings/{meeting_id}", headers=AUTH).status_code == 404


def test_websocket_streams_status(client: TestClient) -> None:
    with client.websocket_connect("/ws?token=test-token") as ws:
        message = ws.receive_json()
        assert message["type"] == "recording"
        assert message["state"] == "idle"


def test_websocket_rejects_bad_token(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=nope") as ws:
            ws.receive_json()
