from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from minutemind import store
from minutemind.api import TOKEN_HEADER, create_app
from minutemind.session import RecordingSession

from conftest import (
    LOOPBACK,
    MIC,
    OTHERS_SAID,
    YOU_SAID,
    FakeEngine,
    FakeHardwareCache,
    FakeRecorder,
    fake_devices,
)

AUTH = {TOKEN_HEADER: "test-token"}


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def client(db: store.Store, engine: FakeEngine) -> Iterator[TestClient]:
    session = RecordingSession(db, recorder_factory=FakeRecorder, device_lister=fake_devices)
    app = create_app(
        "test-token", db=db, session=session, engine=engine, hardware_cache=FakeHardwareCache()
    )
    with TestClient(app) as c:
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


def test_cors_preflight_from_dev_server(client: TestClient) -> None:
    """In `npm run dev` the UI runs on http://localhost:5173, another origin."""
    res = client.options(
        "/audio/devices",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-minutemind-token",
        },
    )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "x-minutemind-token" in res.headers["access-control-allow-headers"].lower()

    real = client.get("/health", headers={**AUTH, "Origin": "http://localhost:5173"})
    assert real.status_code == 200
    assert real.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_rejects_other_origins(client: TestClient) -> None:
    res = client.options(
        "/audio/devices",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in res.headers
    # And the token is still required even from an allowed origin.
    assert client.get("/health", headers={"Origin": "http://localhost:5173"}).status_code == 401


def test_start_with_all_outputs(client: TestClient) -> None:
    res = client.post(
        "/recording/start", json={"mic_device_id": MIC.id, "system_device_id": "all"}, headers=AUTH
    )
    assert res.status_code == 200
    assert res.json()["system_device"].startswith("All outputs (")
    bad = {"system_device_id": "everything"}
    client.post("/recording/stop", headers=AUTH)
    assert client.post("/recording/start", json=bad, headers=AUTH).status_code == 422


def _record(client: TestClient) -> int:
    body = {"mic_device_id": MIC.id, "system_device_id": "all"}
    client.post("/recording/start", json=body, headers=AUTH)
    return client.post("/recording/stop", headers=AUTH).json()["id"]


def test_transcribes_automatically_after_recording(client: TestClient) -> None:
    meeting_id = _record(client)
    assert client.app.state.transcriber.wait_idle()
    transcript = client.get(f"/meetings/{meeting_id}/transcript", headers=AUTH).json()
    assert transcript["status"] == "done"
    assert transcript["device"] == "GPU"
    assert transcript["model"] == "fake-whisper"
    assert [(s["source"], s["text"]) for s in transcript["segments"]] == [
        ("mic", YOU_SAID),
        ("system", OTHERS_SAID),
    ]
    assert "words" not in transcript["segments"][0]


def test_auto_transcribe_can_be_turned_off(client: TestClient) -> None:
    res = client.patch("/settings", json={"auto_transcribe": False}, headers=AUTH)
    assert res.json() == {"processing": "auto", "auto_transcribe": False}
    meeting_id = _record(client)
    assert client.app.state.transcriber.wait_idle()
    status = client.get(f"/meetings/{meeting_id}/transcript", headers=AUTH).json()["status"]
    assert status == "none"

    # ...and a transcript can still be requested by hand.
    res = client.post(f"/meetings/{meeting_id}/transcribe", headers=AUTH)
    assert res.status_code == 202
    assert client.app.state.transcriber.wait_idle()
    status = client.get(f"/meetings/{meeting_id}/transcript", headers=AUTH).json()["status"]
    assert status == "done"


def test_transcribe_rejects_bad_requests(client: TestClient) -> None:
    assert client.post("/meetings/999/transcribe", headers=AUTH).status_code == 404
    client.post("/recording/start", json={"mic_device_id": MIC.id}, headers=AUTH)
    meeting_id = client.get("/recording", headers=AUTH).json()["meeting"]["id"]
    assert client.post(f"/meetings/{meeting_id}/transcribe", headers=AUTH).status_code == 409


def test_settings_validation(client: TestClient) -> None:
    assert client.get("/settings", headers=AUTH).json() == {
        "processing": "auto",
        "auto_transcribe": True,
    }
    assert client.patch("/settings", json={"processing": "gpu"}, headers=AUTH).json()[
        "processing"
    ] == "gpu"
    assert client.patch("/settings", json={"processing": "fast"}, headers=AUTH).status_code == 422


def test_hardware_endpoint(client: TestClient) -> None:
    data = client.get("/system/hardware", headers=AUTH).json()
    assert data["info"]["gpu_name"] == "Test GPU"
    assert data["profile"]["name"] == "gpu"
    client.patch("/settings", json={"processing": "cpu"}, headers=AUTH)
    assert client.get("/system/hardware", headers=AUTH).json()["profile"]["name"] == "cpu"
    assert data["model"]["name"] == "large-v3-turbo"


def test_websocket_streams_transcription_status(client: TestClient) -> None:
    with client.websocket_connect("/ws?token=test-token") as ws:
        types = {ws.receive_json()["type"] for _ in range(2)}
    assert types == {"recording", "transcription"}


def test_deleting_a_meeting_deletes_its_transcript(client: TestClient, db: store.Store) -> None:
    meeting_id = _record(client)
    assert client.app.state.transcriber.wait_idle()
    assert db.get_segments(meeting_id)
    client.delete(f"/meetings/{meeting_id}", headers=AUTH)
    assert db.get_segments(meeting_id) == []
