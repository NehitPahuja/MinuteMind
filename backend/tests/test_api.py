from __future__ import annotations

from typing import Iterator

import numpy as np

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from minutemind import audio_files, store
from minutemind.api import TOKEN_HEADER, create_app
from minutemind.session import RecordingSession

from conftest import (
    LOOPBACK,
    MIC,
    OTHERS_SAID,
    YOU_SAID,
    LIVE_YOU,
    FakeDetector,
    FakeEngine,
    FakeHardwareCache,
    FakeLanguageModel,
    FakeLiveEngine,
    FakeRecorder,
    fake_devices,
)

AUTH = {TOKEN_HEADER: "test-token"}


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def live_engine() -> FakeLiveEngine:
    return FakeLiveEngine()


@pytest.fixture
def detector() -> FakeDetector:
    return FakeDetector()


@pytest.fixture
def llm() -> FakeLanguageModel:
    return FakeLanguageModel()


@pytest.fixture
def client(
    db: store.Store,
    engine: FakeEngine,
    live_engine: FakeLiveEngine,
    detector: FakeDetector,
    llm: FakeLanguageModel,
) -> Iterator[TestClient]:
    session = RecordingSession(db, recorder_factory=FakeRecorder, device_lister=fake_devices)
    app = create_app(
        "test-token",
        db=db,
        session=session,
        engine=engine,
        hardware_cache=FakeHardwareCache(),
        live_engine=live_engine,
        live_chunk_s=1.0,
        detector=detector,
        language_model=llm,
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
    assert res.json() == {
        "processing": "auto",
        "auto_transcribe": False,
        "live_transcript": True,
        "speaker_labels": True,
        "auto_minutes": True,
    }
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
        "live_transcript": True,
        "speaker_labels": True,
        "auto_minutes": True,
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
        types = {ws.receive_json()["type"] for _ in range(3)}
    assert types == {"recording", "transcription", "live"}


def test_deleting_a_meeting_deletes_its_transcript(client: TestClient, db: store.Store) -> None:
    meeting_id = _record(client)
    assert client.app.state.transcriber.wait_idle()
    assert db.get_segments(meeting_id)
    client.delete(f"/meetings/{meeting_id}", headers=AUTH)
    assert db.get_segments(meeting_id) == []


# -- notes ---------------------------------------------------------------------


def test_notes_during_and_after_a_recording(client: TestClient, db: store.Store) -> None:
    client.post("/recording/start", json={"mic_device_id": MIC.id}, headers=AUTH)
    meeting_id = client.get("/recording", headers=AUTH).json()["meeting"]["id"]

    # While recording, the note is stamped with the current recording time.
    first = client.post(f"/meetings/{meeting_id}/notes", json={"text": " Ask about budget "}, headers=AUTH)
    assert first.status_code == 201
    assert first.json()["text"] == "Ask about budget"
    assert 0 <= first.json()["ts_ms"] < 5000

    client.post("/recording/stop", headers=AUTH)
    # Afterwards the time must be given (e.g. the player position).
    assert client.post(
        f"/meetings/{meeting_id}/notes", json={"text": "Later"}, headers=AUTH
    ).status_code == 422
    client.post(f"/meetings/{meeting_id}/notes", json={"text": "Early", "ts_ms": 0}, headers=AUTH)

    notes = client.get(f"/meetings/{meeting_id}/notes", headers=AUTH).json()
    assert [n["text"] for n in notes] == ["Early", "Ask about budget"] or [
        n["text"] for n in notes
    ] == ["Ask about budget", "Early"]
    assert notes == sorted(notes, key=lambda n: (n["ts_ms"], n["id"]))

    note_id = first.json()["id"]
    edited = client.patch(f"/notes/{note_id}", json={"text": "Budget: 420/month", "ts_ms": 9000}, headers=AUTH)
    assert edited.json()["text"] == "Budget: 420/month" and edited.json()["ts_ms"] == 9000
    assert client.patch(f"/notes/{note_id}", json={"text": ""}, headers=AUTH).status_code == 422
    assert client.delete(f"/notes/{note_id}", headers=AUTH).status_code == 204
    assert client.delete(f"/notes/{note_id}", headers=AUTH).status_code == 404
    assert client.patch("/notes/999", json={"text": "x"}, headers=AUTH).status_code == 404

    client.delete(f"/meetings/{meeting_id}", headers=AUTH)
    assert db.list_notes(meeting_id) == []  # notes go with the meeting


# -- live transcript -------------------------------------------------------------


def test_live_transcript_streams_over_the_websocket(
    client: TestClient, live_engine: FakeLiveEngine
) -> None:
    client.post("/recording/start", json={"mic_device_id": MIC.id}, headers=AUTH)
    live = client.app.state.live
    # The fake recorder doesn't produce audio, so feed the live tap directly.
    speech = np.full(int(audio_files.SAMPLE_RATE * 2.5), 0.1, np.float32)
    live.feed("mic", 0, speech)

    lines: list[dict] = []
    with client.websocket_connect("/ws?token=test-token") as ws:
        for _ in range(300):
            message = ws.receive_json()
            if message["type"] == "live":
                lines.extend(message["lines"])
                if lines:
                    break
    assert lines and lines[0]["source"] == "mic" and lines[0]["text"] == LIVE_YOU
    assert live_engine.prepared == ["small"]  # the GPU profile uses the small model

    client.post("/recording/stop", headers=AUTH)
    assert live.snapshot()["state"] == "off"
    assert live_engine.releases >= 1  # unloaded so the final pass gets the GPU


def test_live_transcript_can_be_turned_off(client: TestClient, live_engine: FakeLiveEngine) -> None:
    client.patch("/settings", json={"live_transcript": False}, headers=AUTH)
    client.post("/recording/start", json={"mic_device_id": MIC.id}, headers=AUTH)
    assert client.app.state.live.snapshot()["state"] == "off"
    client.post("/recording/stop", headers=AUTH)
    assert live_engine.prepared == []


def test_final_transcripts_wait_while_recording(client: TestClient) -> None:
    client.post("/recording/start", json={"mic_device_id": MIC.id}, headers=AUTH)
    assert client.app.state.transcriber.status()["paused"] is True
    client.post("/recording/stop", headers=AUTH)
    assert client.app.state.transcriber.status()["paused"] is False
    assert client.app.state.transcriber.wait_idle()


# -- speakers --------------------------------------------------------------------


def test_transcript_includes_speakers(client: TestClient, detector: FakeDetector) -> None:
    meeting_id = _record(client)
    assert client.app.state.transcriber.wait_idle()
    data = client.get(f"/meetings/{meeting_id}/transcript", headers=AUTH).json()
    assert data["speakers_status"] == "done"
    assert data["speakers"] == [{"label": "S1", "display_name": "Speaker 1", "lines": 1}]
    by_source = {s["source"]: s["speaker"] for s in data["segments"]}
    assert by_source == {"mic": None, "system": "S1"}
    assert detector.calls == [None]


def test_rename_speaker(client: TestClient) -> None:
    meeting_id = _record(client)
    assert client.app.state.transcriber.wait_idle()
    res = client.patch(
        f"/meetings/{meeting_id}/speakers/S1", json={"display_name": " Priya "}, headers=AUTH
    )
    assert res.status_code == 200
    assert res.json()[0]["display_name"] == "Priya"
    speakers = client.get(f"/meetings/{meeting_id}/transcript", headers=AUTH).json()["speakers"]
    assert speakers[0]["display_name"] == "Priya"
    assert client.patch(
        f"/meetings/{meeting_id}/speakers/S9", json={"display_name": "X"}, headers=AUTH
    ).status_code == 404
    assert client.patch(
        f"/meetings/{meeting_id}/speakers/S1", json={"display_name": ""}, headers=AUTH
    ).status_code == 422


def test_detect_speakers_again_with_a_count(client: TestClient, detector: FakeDetector) -> None:
    client.patch("/settings", json={"auto_transcribe": False}, headers=AUTH)
    meeting_id = _record(client)
    # Not transcribed yet.
    assert client.post(
        f"/meetings/{meeting_id}/speakers/detect", json={}, headers=AUTH
    ).status_code == 409
    client.post(f"/meetings/{meeting_id}/transcribe", headers=AUTH)
    assert client.app.state.transcriber.wait_idle()

    res = client.post(
        f"/meetings/{meeting_id}/speakers/detect", json={"num_speakers": 3}, headers=AUTH
    )
    assert res.status_code == 202
    assert client.app.state.transcriber.wait_idle()
    assert detector.calls == [None, 3]
    assert client.post(
        f"/meetings/{meeting_id}/speakers/detect", json={"num_speakers": 0}, headers=AUTH
    ).status_code == 422


def test_speaker_labels_can_be_turned_off(client: TestClient, detector: FakeDetector) -> None:
    client.patch("/settings", json={"speaker_labels": False}, headers=AUTH)
    meeting_id = _record(client)
    assert client.app.state.transcriber.wait_idle()
    data = client.get(f"/meetings/{meeting_id}/transcript", headers=AUTH).json()
    assert data["status"] == "done"
    assert data["speakers_status"] == "none"
    assert data["speakers"] == []
    assert detector.calls == []


# -- minutes ---------------------------------------------------------------------


def test_minutes_are_written_after_transcribing(client: TestClient, llm: FakeLanguageModel) -> None:
    meeting_id = _record(client)
    assert client.app.state.transcriber.wait_idle()
    data = client.get(f"/meetings/{meeting_id}/minutes", headers=AUTH).json()
    assert data["status"] == "done"
    assert data["error"] is None
    assert data["model"] == "fake-llm"
    assert data["device"] == "GPU"
    assert data["minutes"]["decisions"] == ["The launch moves to March."]
    assert data["minutes"]["action_items"] == [
        {"task": "Send the hosting quote", "owner": "Others", "due": "Friday"}
    ]
    assert data["minutes"]["topics"] == [{"title": "Launch date", "start_ms": 0}]
    # The transcript and the speaker's name reached the model.
    assert YOU_SAID in llm.prompts[0] and "Speaker 1:" in llm.prompts[0]

    md = client.get(f"/meetings/{meeting_id}/minutes.md", headers=AUTH)
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert "## Decisions\n\n- The launch moves to March." in md.text
    assert "- [ ] Send the hosting quote (**Others**, due Friday)" in md.text


def test_minutes_can_be_turned_off_and_written_by_hand(
    client: TestClient, llm: FakeLanguageModel
) -> None:
    client.patch("/settings", json={"auto_minutes": False}, headers=AUTH)
    meeting_id = _record(client)
    assert client.app.state.transcriber.wait_idle()
    data = client.get(f"/meetings/{meeting_id}/minutes", headers=AUTH).json()
    assert data["status"] == "none" and data["minutes"] is None
    assert llm.prompts == []
    assert client.get(f"/meetings/{meeting_id}/minutes.md", headers=AUTH).status_code == 404

    res = client.post(f"/meetings/{meeting_id}/minutes", headers=AUTH)
    assert res.status_code == 202
    assert client.app.state.transcriber.wait_idle()
    data = client.get(f"/meetings/{meeting_id}/minutes", headers=AUTH).json()
    assert data["status"] == "done"
    assert data["minutes"]["summary"].startswith("The team agreed")


def test_minutes_rejects_bad_requests(client: TestClient) -> None:
    assert client.post("/meetings/999/minutes", headers=AUTH).status_code == 404
    assert client.get("/meetings/999/minutes", headers=AUTH).status_code == 404
    client.patch("/settings", json={"auto_transcribe": False}, headers=AUTH)
    meeting_id = _record(client)
    res = client.post(f"/meetings/{meeting_id}/minutes", headers=AUTH)
    assert res.status_code == 409
    assert res.json()["detail"] == "Transcribe the meeting first"


def test_minutes_failure_keeps_the_transcript(client: TestClient, llm: FakeLanguageModel) -> None:
    llm.error = ValueError("model exploded")
    meeting_id = _record(client)
    assert client.app.state.transcriber.wait_idle()
    assert client.get(f"/meetings/{meeting_id}/transcript", headers=AUTH).json()["status"] == "done"
    data = client.get(f"/meetings/{meeting_id}/minutes", headers=AUTH).json()
    assert data["status"] == "failed"
    assert "model exploded" in data["error"]


def test_hardware_endpoint_reports_the_language_model(client: TestClient) -> None:
    data = client.get("/system/hardware", headers=AUTH).json()
    assert data["llm"] == {"name": "fake-llm", "local": True, "downloaded": True}
