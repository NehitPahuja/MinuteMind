"""FastAPI application for the MinuteMind local service.

Every request must carry the per-launch session token that Electron generates,
so other programs on the machine cannot drive the service. HTTP clients send it
in the X-MinuteMind-Token header; <audio> elements and WebSockets, which can't
set headers, pass it as a `token` query parameter instead.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from minutemind import __version__, audio_files, diarize, hardware, minutes, models, paths, store
from minutemind.llm.base import LanguageModel
from minutemind.capture import list_devices
from minutemind.session import RecordingError, RecordingSession
from minutemind.live import LiveEngine, LiveTranscriber, LiveWhisper
from minutemind.transcribe import Engine, TranscriptionQueue, WhisperEngine

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-MinuteMind-Token"

# In development the UI is served by Vite from http://localhost:<port>, a
# different origin from this service, so the browser sends CORS preflight
# requests. Only local origins are allowed, and the token is still required.
LOCAL_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
LEVELS_INTERVAL_S = 0.08


class StartRecording(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    mic_device_id: int | None = None
    # A loopback device id, "all" to record every output device, or null.
    system_device_id: int | Literal["all"] | None = None


class RenameMeeting(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class SettingsUpdate(BaseModel):
    processing: Literal["auto", "gpu", "cpu"] | None = None
    auto_transcribe: bool | None = None
    live_transcript: bool | None = None
    speaker_labels: bool | None = None
    auto_minutes: bool | None = None


class RenameSpeaker(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)


class DetectSpeakers(BaseModel):
    # How many people were on the other side, if known; omit to detect it.
    num_speakers: int | None = Field(default=None, ge=1, le=20)


class NewNote(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    # Moment in the meeting. Omit while recording to use "now".
    ts_ms: int | None = Field(default=None, ge=0)


class NoteUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=5000)
    ts_ms: int | None = Field(default=None, ge=0)


class HardwareCache:
    """Hardware detection is slow (nvidia-smi, loading CUDA), so do it once."""

    def __init__(self) -> None:
        self._info: hardware.HardwareInfo | None = None
        self._lock = threading.Lock()

    def info(self, refresh: bool = False) -> hardware.HardwareInfo:
        with self._lock:
            if self._info is None or refresh:
                self._info = hardware.detect()
            return self._info


def create_app(
    token: str,
    db: store.Store | None = None,
    session: RecordingSession | None = None,
    engine: Engine | None = None,
    hardware_cache: HardwareCache | None = None,
    live_engine: LiveEngine | None = None,
    live_chunk_s: float | None = None,
    detector: diarize.Detector | None = None,
    language_model: LanguageModel | None = None,
) -> FastAPI:
    if not token:
        raise ValueError("A non-empty session token is required")

    def require_token(conn: HTTPConnection) -> None:
        supplied = conn.headers.get(TOKEN_HEADER) or conn.query_params.get("token")
        if supplied is None or not secrets.compare_digest(supplied, token):
            if conn.scope["type"] == "websocket":
                raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db or store.Store()
        app.state.session = session or RecordingSession(app.state.db)
        app.state.hardware = hardware_cache or HardwareCache()

        def current_profile() -> hardware.Profile:
            preference = str(app.state.db.get_settings()["processing"])
            return hardware.choose_profile(app.state.hardware.info(), preference)

        # One manager for every model, so only one is ever loaded at a time.
        manager = getattr(engine, "manager", None) or models.ModelManager()
        if language_model is None:
            from minutemind.llm.llamacpp import LlamaCppModel

            llm: LanguageModel = LlamaCppModel(manager)
        else:
            llm = language_model
        app.state.llm = llm
        app.state.transcriber = TranscriptionQueue(
            app.state.db,
            engine or WhisperEngine(manager),
            current_profile,
            detector=detector or diarize.SherpaDetector(),
            speakers_enabled=lambda: bool(app.state.db.get_settings()["speaker_labels"]),
            writer=minutes.MinutesWriter(llm),
            minutes_enabled=lambda: bool(app.state.db.get_settings()["auto_minutes"]),
        )
        app.state.profile = current_profile

        live_kwargs = {"chunk_s": live_chunk_s} if live_chunk_s else {}
        app.state.live = LiveTranscriber(
            live_engine or LiveWhisper(manager),
            current_profile,
            gpu_free=lambda: not app.state.transcriber.busy(),
            **live_kwargs,
        )

        def queue_if_enabled(meeting: store.Meeting) -> None:
            if app.state.db.get_settings()["auto_transcribe"]:
                app.state.transcriber.enqueue(meeting.id)

        session_ = app.state.session
        session_.on_finished = queue_if_enabled
        session_.live = app.state.live
        session_.live_enabled = lambda: bool(app.state.db.get_settings()["live_transcript"])
        # While recording, final transcripts wait so the live model gets the GPU.
        session_.on_started = lambda _meeting: app.state.transcriber.pause()
        session_.on_stopped = app.state.transcriber.resume
        recovered = app.state.session.recover_interrupted()
        if recovered:
            log.warning("Recovered interrupted recordings: %s", recovered)
        app.state.transcriber.start()
        yield
        app.state.session.shutdown()
        app.state.transcriber.stop()

    app = FastAPI(
        title="MinuteMind",
        version=__version__,
        dependencies=[Depends(require_token)],
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Preflight (OPTIONS) requests carry no token; this middleware answers them
    # before the token check runs.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=LOCAL_ORIGIN_REGEX,
        allow_methods=["*"],
        allow_headers=[TOKEN_HEADER, "Content-Type"],
    )

    def get_session() -> RecordingSession:
        return app.state.session

    def get_db() -> store.Store:
        return app.state.db

    def get_meeting_or_404(meeting_id: int) -> store.Meeting:
        meeting = get_db().get_meeting(meeting_id)
        if meeting is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
        return meeting

    # -- health ------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # -- audio devices -----------------------------------------------------

    @app.get("/audio/devices")
    def audio_devices() -> dict:
        try:
            devices = list_devices()
        except Exception as exc:  # noqa: BLE001 - surface driver problems to the UI
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Audio unavailable: {exc}")
        return {kind: [d.to_dict() for d in items] for kind, items in devices.items()}

    # -- recording ---------------------------------------------------------

    @app.get("/recording")
    def recording_status() -> dict:
        return get_session().status()

    @app.post("/recording/start")
    def recording_start(body: StartRecording) -> dict:
        try:
            meeting = get_session().start(body.title, body.mic_device_id, body.system_device_id)
        except RecordingError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
        return meeting.to_dict()

    @app.post("/recording/pause")
    def recording_pause() -> dict:
        return _action(get_session().pause)

    @app.post("/recording/resume")
    def recording_resume() -> dict:
        return _action(get_session().resume)

    @app.post("/recording/stop")
    def recording_stop() -> dict:
        try:
            return get_session().stop().to_dict()
        except RecordingError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))

    def _action(fn) -> dict:
        try:
            fn()
        except RecordingError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
        return get_session().status()

    # -- meetings ----------------------------------------------------------

    @app.get("/meetings")
    def meetings() -> list[dict]:
        return [m.to_dict() for m in get_db().list_meetings()]

    @app.get("/meetings/{meeting_id}")
    def meeting(meeting_id: int) -> dict:
        return get_meeting_or_404(meeting_id).to_dict()

    @app.patch("/meetings/{meeting_id}")
    def rename(meeting_id: int, body: RenameMeeting) -> dict:
        get_meeting_or_404(meeting_id)
        updated = get_db().rename_meeting(meeting_id, body.title.strip())
        assert updated is not None
        return updated.to_dict()

    @app.delete("/meetings/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete(meeting_id: int) -> None:
        get_meeting_or_404(meeting_id)
        if get_session().is_recording(meeting_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "Stop the recording before deleting it")
        get_db().delete_meeting(meeting_id)

    # -- transcripts -------------------------------------------------------

    @app.post("/meetings/{meeting_id}/transcribe", status_code=status.HTTP_202_ACCEPTED)
    def transcribe(meeting_id: int) -> dict:
        meeting = get_meeting_or_404(meeting_id)
        if get_session().is_recording(meeting_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "Stop the recording first")
        if not (meeting.has_mic or meeting.has_system):
            raise HTTPException(status.HTTP_409_CONFLICT, "This meeting has no audio")
        app.state.transcriber.enqueue(meeting_id)
        return get_meeting_or_404(meeting_id).to_dict()

    @app.get("/meetings/{meeting_id}/transcript")
    def transcript(meeting_id: int) -> dict:
        meeting = get_meeting_or_404(meeting_id)
        return {
            "status": meeting.transcript_status,
            "error": meeting.transcript_error,
            "model": meeting.transcript_model,
            "device": meeting.transcript_device,
            "speakers_status": meeting.speakers_status,
            "speakers_error": meeting.speakers_error,
            "speakers": get_db().list_speakers(meeting_id),
            "segments": [s.to_dict() for s in get_db().get_segments(meeting_id)],
        }

    @app.patch("/meetings/{meeting_id}/speakers/{label}")
    def rename_speaker(meeting_id: int, label: str, body: RenameSpeaker) -> list[dict]:
        get_meeting_or_404(meeting_id)
        if not get_db().rename_speaker(meeting_id, label, body.display_name.strip()):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Speaker not found")
        return get_db().list_speakers(meeting_id)

    @app.post("/meetings/{meeting_id}/speakers/detect", status_code=status.HTTP_202_ACCEPTED)
    def detect_speakers(meeting_id: int, body: DetectSpeakers) -> dict:
        meeting = get_meeting_or_404(meeting_id)
        if meeting.transcript_status != store.TRANSCRIPT_DONE:
            raise HTTPException(status.HTTP_409_CONFLICT, "Transcribe the meeting first")
        if not app.state.transcriber.enqueue_speakers(meeting_id, body.num_speakers):
            raise HTTPException(status.HTTP_409_CONFLICT, "This meeting is already being processed")
        return {"queued": True}

    # -- minutes -------------------------------------------------------------

    @app.get("/meetings/{meeting_id}/minutes")
    def get_minutes(meeting_id: int) -> dict:
        meeting = get_meeting_or_404(meeting_id)
        saved = get_db().get_minutes(meeting_id)
        return {
            "status": meeting.minutes_status,
            "error": meeting.minutes_error,
            "minutes": saved["content"] if saved else None,
            "model": saved["model"] if saved else None,
            "device": saved["device"] if saved else None,
            "created_at": saved["created_at"] if saved else None,
        }

    @app.post("/meetings/{meeting_id}/minutes", status_code=status.HTTP_202_ACCEPTED)
    def write_minutes(meeting_id: int) -> dict:
        meeting = get_meeting_or_404(meeting_id)
        if meeting.transcript_status != store.TRANSCRIPT_DONE:
            raise HTTPException(status.HTTP_409_CONFLICT, "Transcribe the meeting first")
        if not get_db().get_segments(meeting_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "Nothing was said in this meeting")
        if not app.state.transcriber.enqueue_minutes(meeting_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "This meeting is already being processed")
        return get_meeting_or_404(meeting_id).to_dict()

    @app.get("/meetings/{meeting_id}/minutes.md", response_class=PlainTextResponse)
    def minutes_markdown(meeting_id: int) -> PlainTextResponse:
        meeting = get_meeting_or_404(meeting_id)
        saved = get_db().get_minutes(meeting_id)
        if saved is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "This meeting has no minutes yet")
        return PlainTextResponse(
            minutes.to_markdown(meeting, saved["content"]), media_type="text/markdown"
        )

    # -- notes ---------------------------------------------------------------

    @app.get("/meetings/{meeting_id}/notes")
    def list_notes(meeting_id: int) -> list[dict]:
        get_meeting_or_404(meeting_id)
        return [n.to_dict() for n in get_db().list_notes(meeting_id)]

    @app.post("/meetings/{meeting_id}/notes", status_code=status.HTTP_201_CREATED)
    def add_note(meeting_id: int, body: NewNote) -> dict:
        get_meeting_or_404(meeting_id)
        ts_ms = body.ts_ms
        if ts_ms is None:
            ts_ms = get_session().elapsed_ms(meeting_id)
        if ts_ms is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "ts_ms is required when the meeting isn't being recorded",
            )
        return get_db().add_note(meeting_id, ts_ms, body.text.strip()).to_dict()

    @app.patch("/notes/{note_id}")
    def update_note(note_id: int, body: NoteUpdate) -> dict:
        if get_db().get_note(note_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
        text = body.text.strip() if body.text is not None else None
        note = get_db().update_note(note_id, text=text, ts_ms=body.ts_ms)
        assert note is not None
        return note.to_dict()

    @app.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_note(note_id: int) -> None:
        if not get_db().delete_note(note_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")

    # -- system and settings -------------------------------------------------

    @app.get("/system/hardware")
    def system_hardware(refresh: bool = False) -> dict:
        info = app.state.hardware.info(refresh=refresh)
        return {
            "info": info.to_dict(),
            "profile": app.state.profile().to_dict(),
            "model": {
                "name": models.TRANSCRIBE_MODEL,
                "downloaded": models.is_downloaded(models.TRANSCRIBE_MODEL),
            },
            "llm": {
                "name": app.state.llm.name,
                "local": app.state.llm.local,
                "downloaded": _llm_downloaded(app.state.llm),
            },
        }

    @app.get("/settings")
    def get_settings() -> dict:
        return get_db().get_settings()

    @app.patch("/settings")
    def update_settings(body: SettingsUpdate) -> dict:
        changes = body.model_dump(exclude_none=True)
        return get_db().update_settings(changes)

    @app.get("/meetings/{meeting_id}/audio/{track}")
    def audio(meeting_id: int, track: str) -> FileResponse:
        get_meeting_or_404(meeting_id)
        if track not in audio_files.TRACK_FILES:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown track")
        if get_session().is_recording(meeting_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "Still recording")
        path = paths.meeting_dir(meeting_id) / audio_files.TRACK_FILES[track]
        if not path.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No audio for this track")
        return FileResponse(path, media_type="audio/wav")

    # -- live updates ------------------------------------------------------

    @app.websocket("/ws")
    async def live(ws: WebSocket) -> None:
        await ws.accept()
        # The live transcript is sent incrementally: only lines this client
        # hasn't seen yet, restarting when a new recording begins.
        seen_meeting: int | None = None
        sent = 0
        try:
            while True:
                await ws.send_json({"type": "recording", **get_session().status()})
                await ws.send_json({"type": "transcription", **app.state.transcriber.status()})
                snap = app.state.live.snapshot()
                reset = snap["meeting_id"] != seen_meeting
                if reset:
                    seen_meeting, sent = snap["meeting_id"], 0
                fresh = app.state.live.snapshot(sent)
                await ws.send_json(
                    {
                        "type": "live",
                        "meeting_id": fresh["meeting_id"],
                        "state": fresh["state"],
                        "detail": fresh["detail"],
                        "reset": reset,
                        "lines": fresh["lines"],
                    }
                )
                sent += len(fresh["lines"])
                await asyncio.sleep(LEVELS_INTERVAL_S)
        except (WebSocketDisconnect, RuntimeError):
            return

    return app


def _llm_downloaded(llm: LanguageModel) -> bool:
    spec = getattr(llm, "spec", None)
    return models.is_llm_downloaded(spec) if spec is not None else True
