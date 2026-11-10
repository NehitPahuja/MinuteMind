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
from contextlib import asynccontextmanager

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
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from minutemind import __version__, audio_files, paths, store
from minutemind.capture import list_devices
from minutemind.session import RecordingError, RecordingSession

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
    system_device_id: int | None = None


class RenameMeeting(BaseModel):
    title: str = Field(min_length=1, max_length=200)


def create_app(
    token: str,
    db: store.Store | None = None,
    session: RecordingSession | None = None,
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
        recovered = app.state.session.recover_interrupted()
        if recovered:
            log.warning("Recovered interrupted recordings: %s", recovered)
        yield
        app.state.session.shutdown()

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
        try:
            while True:
                await ws.send_json({"type": "recording", **get_session().status()})
                await asyncio.sleep(LEVELS_INTERVAL_S)
        except (WebSocketDisconnect, RuntimeError):
            return

    return app
