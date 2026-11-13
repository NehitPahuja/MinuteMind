"""Background transcription of finished meetings.

Each meeting's two tracks are transcribed separately: the mic track becomes
"you" and the PC-sound track "others", so every line already says who spoke.
When laptop speakers leak call audio into the microphone, the same words show
up on both tracks; those duplicate mic lines are dropped.

One job runs at a time on a worker thread. Progress is exposed through
`status()` and streamed to the UI over the /ws WebSocket.
"""

from __future__ import annotations

import difflib
import logging
import queue
import re
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from minutemind import audio_files, diarize, hardware, models, paths, store

log = logging.getLogger(__name__)

SOURCES = (("mic", audio_files.MIC_FILE), ("system", audio_files.SYSTEM_FILE))

# A track whose loudest sample is below this is treated as empty (int16 scale).
SILENT_PEAK = 200
# Mic lines this similar to an overlapping PC-sound line are speaker bleed.
BLEED_SIMILARITY = 0.6
BLEED_SLACK_MS = 1500


@dataclass(frozen=True)
class RawSegment:
    start: float  # seconds
    end: float
    text: str
    words: list[tuple[float, float, str]]


class Engine(Protocol):
    """What the queue needs from a speech-to-text implementation."""

    model_name: str

    def ensure_model(self, on_progress: Callable[[float], None]) -> None: ...

    def transcribe(
        self,
        audio: np.ndarray,
        profile: hardware.Profile,
        on_progress: Callable[[float], None],
    ) -> list[RawSegment]: ...

    def release(self) -> None: ...


class WhisperEngine:
    """faster-whisper running the large-v3-turbo model."""

    def __init__(self, manager: models.ModelManager, model_name: str = models.TRANSCRIBE_MODEL):
        self.manager = manager
        self.model_name = model_name

    def ensure_model(self, on_progress: Callable[[float], None]) -> None:
        models.download_whisper(self.model_name, on_progress)

    def transcribe(
        self,
        audio: np.ndarray,
        profile: hardware.Profile,
        on_progress: Callable[[float], None],
    ) -> list[RawSegment]:
        model = self.manager.whisper(self.model_name, profile.device, profile.compute_type)
        segments, _info = model.transcribe(
            audio,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            word_timestamps=True,
            # Prevents the runaway repetition Whisper can fall into on long audio.
            condition_on_previous_text=False,
        )
        out: list[RawSegment] = []
        for seg in segments:  # a generator: decoding happens as we iterate
            on_progress(seg.end)
            text = seg.text.strip()
            if text:
                words = [(w.start, w.end, w.word) for w in (seg.words or [])]
                out.append(RawSegment(seg.start, seg.end, text, words))
        return out

    def release(self) -> None:
        self.manager.unload()


def is_gpu_failure(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("out of memory", "cuda", "cublas", "cudnn", "gpu", "device")
    )


class TranscriptionQueue:
    def __init__(
        self,
        db: store.Store,
        engine: Engine,
        profile: Callable[[], hardware.Profile],
        detector: diarize.Detector | None = None,
        speakers_enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self.db = db
        self.engine = engine
        self.detector = detector
        self._speakers_enabled = speakers_enabled
        self._profile = profile
        # Jobs are (meeting_id, kind, num_speakers). kind is "full" (transcribe,
        # then detect speakers) or "speakers" (detect speakers again only).
        self._queue: queue.Queue[tuple[int, str, int | None] | None] = queue.Queue()
        self._queued: list[int] = []
        self._current: dict | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # Cleared while recording, so the live transcript gets the GPU. A job
        # already running finishes; new ones wait.
        self._allowed = threading.Event()
        self._allowed.set()
        self._stopping = False

    # -- control -----------------------------------------------------------

    def start(self) -> None:
        """Start the worker and resume jobs left unfinished by the last run."""
        self._thread = threading.Thread(target=self._work, name="transcriber", daemon=True)
        self._thread.start()
        for meeting_id in self.db.pending_transcripts():
            self.enqueue(meeting_id)

    def pause(self) -> None:
        self._allowed.clear()

    def resume(self) -> None:
        self._allowed.set()

    def busy(self) -> bool:
        """True while a job is using the speech model."""
        with self._lock:
            return self._current is not None

    def stop(self, timeout: float = 5) -> None:
        self._stopping = True
        self._allowed.set()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout)

    def enqueue(self, meeting_id: int) -> bool:
        """Queue a meeting. Returns False if it's already queued or running."""
        if not self._reserve(meeting_id):
            return False
        self.db.set_transcript_status(meeting_id, store.TRANSCRIPT_QUEUED)
        self._queue.put((meeting_id, "full", None))
        return True

    def enqueue_speakers(self, meeting_id: int, num_speakers: int | None = None) -> bool:
        """Queue speaker detection alone (keeps the transcript). `num_speakers`
        tells it how many people were on the other side, if known."""
        if not self._reserve(meeting_id):
            return False
        self._queue.put((meeting_id, "speakers", num_speakers))
        return True

    def _reserve(self, meeting_id: int) -> bool:
        with self._lock:
            running = self._current and self._current["meeting_id"] == meeting_id
            if meeting_id in self._queued or running:
                return False
            self._queued.append(meeting_id)
            return True

    def status(self) -> dict:
        with self._lock:
            return {
                "current": dict(self._current) if self._current else None,
                "queued": list(self._queued),
                "paused": not self._allowed.is_set(),
            }

    def wait_idle(self, timeout: float = 60) -> bool:
        """For tests: block until nothing is queued or running."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if status["current"] is None and not status["queued"]:
                return True
            time.sleep(0.02)
        return False

    # -- worker --------------------------------------------------------------

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            meeting_id, kind, num_speakers = job
            self._allowed.wait()
            if self._stopping:
                return
            with self._lock:
                if meeting_id in self._queued:
                    self._queued.remove(meeting_id)
                self._current = {
                    "meeting_id": meeting_id,
                    "kind": kind,
                    "stage": "starting",
                    "progress": 0.0,
                }
            try:
                if kind == "speakers":
                    self._redo_speakers(meeting_id, num_speakers)
                else:
                    self._run(meeting_id)
            except Exception as exc:  # noqa: BLE001 - report it on the meeting
                log.exception("Job %s for meeting %s failed", kind, meeting_id)
                if kind == "speakers":
                    self.db.set_speakers_status(meeting_id, store.SPEAKERS_FAILED, str(exc))
                else:
                    self.db.set_transcript_status(
                        meeting_id, store.TRANSCRIPT_FAILED, error=str(exc)
                    )
            finally:
                with self._lock:
                    self._current = None
                    idle = not self._queued
                if idle:
                    # Free GPU memory for other apps (and, later, the language model).
                    self.engine.release()

    def _update(self, stage: str, progress: float) -> None:
        with self._lock:
            if self._current is not None:
                self._current["stage"] = stage
                self._current["progress"] = round(max(0.0, min(1.0, progress)), 3)

    def _run(self, meeting_id: int) -> None:
        meeting = self.db.get_meeting(meeting_id)
        if meeting is None:
            return
        self.db.set_transcript_status(meeting_id, store.TRANSCRIPT_RUNNING)

        self._update("downloading", 0.0)
        self.engine.ensure_model(lambda p: self._update("downloading", p))

        folder = paths.meeting_dir(meeting_id)
        tracks: list[tuple[str, np.ndarray]] = []
        for source, filename in SOURCES:
            path = folder / filename
            if not path.exists():
                continue
            samples = audio_files.read_samples(path)
            if samples.size == 0 or int(np.abs(samples).max()) < SILENT_PEAK:
                continue  # nothing was said on this track
            tracks.append((source, samples.astype(np.float32) / 32768.0))

        total_s = sum(len(a) for _, a in tracks) / audio_files.SAMPLE_RATE or 1.0
        done_s = 0.0
        profile = self._profile()
        results: dict[str, list[RawSegment]] = {}

        for source, audio in tracks:
            base = done_s

            def progress(at_s: float, base: float = base) -> None:
                self._update("transcribing", (base + at_s) / total_s)

            self._update("transcribing", base / total_s)
            try:
                results[source] = self.engine.transcribe(audio, profile, progress)
            except Exception as exc:  # noqa: BLE001 - maybe a GPU problem
                if profile.device == "cpu" or not is_gpu_failure(exc):
                    raise
                log.warning("GPU transcription failed (%s); retrying on the CPU", exc)
                self.engine.release()
                profile = hardware.CPU_PROFILE
                results[source] = self.engine.transcribe(audio, profile, progress)
            done_s += len(audio) / audio_files.SAMPLE_RATE

        segments = merge_tracks(results.get("mic", []), results.get("system", []))
        system_audio = dict(tracks).get("system")
        if system_audio is not None and any(s.source == "system" for s in segments):
            # Whisper is done; free the GPU before the (processor-based) speaker step.
            self.engine.release()
            segments = self._label_speakers(meeting_id, segments, system_audio, None)
        else:
            self.db.set_speakers(meeting_id, [])
            self.db.set_speakers_status(meeting_id, store.SPEAKERS_NONE)
        self.db.replace_segments(meeting_id, segments)
        self.db.set_transcript_status(
            meeting_id,
            store.TRANSCRIPT_DONE,
            model=self.engine.model_name,
            device="GPU" if profile.device == "cuda" else "CPU",
        )


    # -- speakers ------------------------------------------------------------

    def _label_speakers(
        self,
        meeting_id: int,
        segments: list[store.Segment],
        system_audio: np.ndarray,
        num_speakers: int | None,
    ) -> list[store.Segment]:
        """Detect who's who on the PC-sound track. Never fails the transcript:
        if detection can't run, lines simply stay labeled "Others"."""
        if self.detector is None or not self._speakers_enabled():
            self.db.set_speakers(meeting_id, [])
            self.db.set_speakers_status(meeting_id, store.SPEAKERS_NONE)
            return segments
        try:
            self._update("speakers", 0.0)
            self.detector.ensure_models(lambda p: self._update("speakers", p * 0.3))
            self._update("speakers", 0.3)
            turns = self.detector.detect(system_audio, num_speakers)
            labeled = diarize.assign_speakers(segments, turns)
        except Exception as exc:  # noqa: BLE001
            log.exception("Speaker detection failed for meeting %s", meeting_id)
            self.db.set_speakers_status(meeting_id, store.SPEAKERS_FAILED, str(exc))
            return segments
        labels = sorted(
            {s.speaker for s in labeled if s.speaker},
            key=lambda label: int(label[1:]),
        )
        self.db.set_speakers(meeting_id, labels)
        self.db.set_speakers_status(meeting_id, store.SPEAKERS_DONE)
        self._update("speakers", 1.0)
        return labeled

    def _redo_speakers(self, meeting_id: int, num_speakers: int | None) -> None:
        path = paths.meeting_dir(meeting_id) / audio_files.SYSTEM_FILE
        segments = self.db.get_segments(meeting_id)
        if not path.exists() or not any(s.source == "system" for s in segments):
            self.db.set_speakers_status(
                meeting_id, store.SPEAKERS_FAILED, "There's no PC sound to tell speakers apart"
            )
            return
        audio = audio_files.read_samples(path).astype(np.float32) / 32768.0
        labeled = self._label_speakers(meeting_id, segments, audio, num_speakers)
        if labeled is not segments:
            self.db.replace_segments(meeting_id, labeled)


# ---------------------------------------------------------------------------
# Combining the two tracks


def merge_tracks(mic: list[RawSegment], system: list[RawSegment]) -> list[store.Segment]:
    """Interleave both tracks by time, dropping mic lines that are just the
    other side of the call leaking from the speakers into the microphone."""
    kept_mic = [seg for seg in mic if not _is_bleed(seg, system)]
    combined = [_to_segment("mic", s) for s in kept_mic] + [_to_segment("system", s) for s in system]
    return sorted(combined, key=lambda s: (s.start_ms, s.source))


def _is_bleed(seg: RawSegment, system: list[RawSegment]) -> bool:
    slack = BLEED_SLACK_MS / 1000
    mine = _normalise(seg.text)
    if not mine:
        return False
    for other in system:
        if other.end + slack < seg.start or other.start - slack > seg.end:
            continue
        ratio = difflib.SequenceMatcher(None, mine, _normalise(other.text)).ratio()
        if ratio >= BLEED_SIMILARITY:
            return True
    return False


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def _to_segment(source: str, seg: RawSegment) -> store.Segment:
    return store.Segment(
        source=source,
        start_ms=int(seg.start * 1000),
        end_ms=int(seg.end * 1000),
        text=seg.text,
        words=[(int(a * 1000), int(b * 1000), w) for a, b, w in seg.words],
    )
