"""Live transcript while recording.

The recorder taps every chunk of 16 kHz audio it writes into a LiveBuffer per
speaker ("mic" = you, "system" = others; several output devices are summed).
A worker thread transcribes roughly 10-second slices with a small, fast
Whisper model and publishes the lines. Slices are cut at the quietest moment
near their end so words aren't split between two slices.

Live lines are a preview only. When the recording stops, the accurate
large-v3-turbo pass (transcribe.py) produces the real transcript.

GPU sharing: the live model only loads when `gpu_free()` says no final
transcription is running (the queue is paused while recording), and it is
unloaded as soon as the recording stops.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Protocol

import numpy as np

from minutemind import audio_files, hardware, models
from minutemind.transcribe import RawSegment, is_gpu_failure

log = logging.getLogger(__name__)

SR = audio_files.SAMPLE_RATE
CHUNK_S = 10.0  # transcribe once this much new audio has arrived
CUT_SEARCH_S = 3.0  # look for a pause in the last few seconds of a chunk
CUT_FRAME_S = 0.2
MARGIN_S = 0.3  # stay this far behind the newest audio (tracks arrive slightly apart)
SILENCE_RMS = 0.003  # a slice quieter than this is skipped without transcribing
POLL_S = 0.5

SOURCES = ("mic", "system")

# Short slices give Whisper's language detector little to go on, so on quiet or
# noisy audio it can jump to a wrong language. The live transcript locks onto
# the first language detected with this confidence and keeps it.
LANGUAGE_LOCK_PROBABILITY = 0.8
# Lines Whisper itself rates as probably not speech are dropped.
NO_SPEECH_THRESHOLD = 0.6


def live_model_for(profile: hardware.Profile) -> str:
    """Small and quick: `small` on a GPU, `base` on the processor.

    MINUTEMIND_LIVE_MODEL overrides it (a development aid, e.g. `tiny`).
    """
    override = os.environ.get("MINUTEMIND_LIVE_MODEL")
    if override:
        return override
    return "small" if profile.device == "cuda" else "base"


class LiveEngine(Protocol):
    def prepare(self, model: str, on_progress: Callable[[float], None]) -> None: ...

    def transcribe(
        self, audio: np.ndarray, model: str, profile: hardware.Profile
    ) -> list[RawSegment]: ...

    def release(self) -> None: ...


class LiveWhisper:
    """faster-whisper tuned for speed rather than accuracy."""

    def __init__(self, manager: models.ModelManager) -> None:
        self.manager = manager
        self.language: str | None = None

    def prepare(self, model: str, on_progress: Callable[[float], None]) -> None:
        self.language = None  # a new recording may be in another language
        models.download_whisper(model, on_progress)

    def transcribe(
        self, audio: np.ndarray, model: str, profile: hardware.Profile
    ) -> list[RawSegment]:
        whisper = self.manager.whisper(model, profile.device, profile.compute_type)
        segments, info = whisper.transcribe(
            audio,
            language=self.language,
            beam_size=1,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            condition_on_previous_text=False,
        )
        lines = [
            RawSegment(s.start, s.end, s.text.strip(), [])
            for s in segments
            if s.text.strip() and s.no_speech_prob < NO_SPEECH_THRESHOLD
        ]
        if self.language is None and lines and info.language_probability >= LANGUAGE_LOCK_PROBABILITY:
            self.language = info.language
            log.info("Live transcript language: %s", self.language)
        return lines

    def release(self) -> None:
        self.manager.unload()


class LiveBuffer:
    """Audio for one speaker on the meeting timeline. Several writers (one
    per output device) can add overlapping audio; it is summed. Audio that has
    been transcribed is dropped to keep memory small."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = np.zeros(0, dtype=np.float32)
        self._base = 0  # timeline index of _data[0]
        self.processed = 0  # timeline index up to which audio was transcribed
        self.newest = 0  # timeline index just past the newest audio received

    def add(self, start: int, frames: np.ndarray) -> None:
        with self._lock:
            end = start + len(frames)
            if end <= self._base:
                return  # already transcribed
            if start < self._base:
                frames = frames[self._base - start :]
                start = self._base
            needed = end - self._base
            if needed > len(self._data):
                grown = np.zeros(max(needed, len(self._data) * 2), dtype=np.float32)
                grown[: len(self._data)] = self._data
                self._data = grown
            self._data[start - self._base : end - self._base] += frames
            self.newest = max(self.newest, end)

    def available(self) -> int:
        """Untranscribed samples that are safely complete."""
        with self._lock:
            return max(0, self.newest - int(MARGIN_S * SR) - self.processed)

    def read(self, start: int, end: int) -> np.ndarray:
        with self._lock:
            lo = max(0, start - self._base)
            hi = max(lo, end - self._base)
            return self._data[lo:hi].copy()

    def advance(self, to: int) -> None:
        """Mark audio up to `to` as transcribed and forget it."""
        with self._lock:
            self.processed = max(self.processed, to)
            drop = self.processed - self._base
            if drop > 0:
                self._data = self._data[drop:].copy()
                self._base = self.processed


def choose_cut(audio: np.ndarray) -> int:
    """Where to end a slice: the middle of the quietest short frame in the
    last few seconds, so a word in progress isn't cut in half."""
    frame = int(CUT_FRAME_S * SR)
    search = int(CUT_SEARCH_S * SR)
    if len(audio) <= search + frame:
        return len(audio)
    best_at, best_rms = len(audio), float("inf")
    for start in range(len(audio) - search, len(audio) - frame + 1, frame // 2):
        rms = float(np.sqrt(np.mean(np.square(audio[start : start + frame]))))
        if rms < best_rms:
            best_at, best_rms = start + frame // 2, rms
    return best_at


class LiveTranscriber:
    def __init__(
        self,
        engine: LiveEngine,
        profile: Callable[[], hardware.Profile],
        gpu_free: Callable[[], bool] = lambda: True,
        chunk_s: float = CHUNK_S,
    ) -> None:
        self.engine = engine
        self._profile = profile
        self._gpu_free = gpu_free
        self.chunk = int(chunk_s * SR)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._meeting_id: int | None = None
        self._buffers: dict[str, LiveBuffer] = {}
        self._lines: list[dict] = []
        self._state = "off"
        self._detail: str | None = None

    # -- control -------------------------------------------------------------

    def start(self, meeting_id: int) -> None:
        self.stop()
        with self._lock:
            self._meeting_id = meeting_id
            self._buffers = {source: LiveBuffer() for source in SOURCES}
            self._lines = []
            self._set("starting")
        self._stop.clear()
        self._thread = threading.Thread(target=self._work, name="live-transcript", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop and unload the live model so the GPU is free for the final pass."""
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(timeout=30)
        self._thread = None
        self.engine.release()
        with self._lock:
            self._set("off")

    def feed(self, kind: str, start: int, frames: np.ndarray) -> None:
        """The recorder's audio tap."""
        buffer = self._buffers.get(kind)
        if buffer is not None:
            buffer.add(start, frames)

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            return {
                "meeting_id": self._meeting_id,
                "state": self._state,
                "detail": self._detail,
                "count": len(self._lines),
                "lines": self._lines[since:],
            }

    # -- worker --------------------------------------------------------------

    def _set(self, state: str, detail: str | None = None) -> None:
        self._state = state
        self._detail = detail

    def _work(self) -> None:
        profile = self._profile()
        model = live_model_for(profile)
        ready = False
        while not self._stop.is_set():
            if not ready:
                if not self._gpu_free():
                    with self._lock:
                        self._set("waiting", "Finishing another transcript first")
                    self._stop.wait(POLL_S)
                    continue
                try:
                    with self._lock:
                        self._set("loading")
                    self.engine.prepare(model, self._download_progress)
                    ready = True
                    with self._lock:
                        self._set("listening")
                except Exception as exc:  # noqa: BLE001 - recording goes on without it
                    log.exception("Live transcript could not start")
                    with self._lock:
                        self._set("error", str(exc))
                    return

            worked = False
            for source in SOURCES:
                if self._stop.is_set():
                    break
                buffer = self._buffers[source]
                if buffer.available() < self.chunk:
                    continue
                start = buffer.processed
                audio = buffer.read(start, start + self.chunk)
                cut = choose_cut(audio)
                audio = audio[:cut]
                worked = True
                if float(np.sqrt(np.mean(np.square(audio)))) < SILENCE_RMS:
                    buffer.advance(start + cut)
                    continue
                try:
                    segments = self.engine.transcribe(audio, model, profile)
                except Exception as exc:  # noqa: BLE001
                    if profile.device == "cuda" and is_gpu_failure(exc):
                        log.warning("Live transcript GPU failure (%s); using the CPU", exc)
                        self.engine.release()
                        profile = hardware.CPU_PROFILE
                        model = live_model_for(profile)
                        ready = False
                        break
                    log.exception("Live transcription of a slice failed")
                    buffer.advance(start + cut)
                    continue
                buffer.advance(start + cut)
                self._publish(source, start, segments)

            if not worked:
                self._stop.wait(POLL_S)

    def _download_progress(self, progress: float) -> None:
        with self._lock:
            if progress < 1.0:
                self._set("loading", f"Downloading the live model {int(progress * 100)}%")

    def _publish(self, source: str, start: int, segments: list[RawSegment]) -> None:
        # Lines are only ever appended, with stable ids, so clients can fetch
        # just the new ones. Mic and PC-sound slices finish in any order, so
        # clients sort by start_ms.
        offset_ms = start * 1000 // SR
        with self._lock:
            for seg in segments:
                self._lines.append(
                    {
                        "id": len(self._lines),
                        "source": source,
                        "start_ms": offset_ms + int(seg.start * 1000),
                        "end_ms": offset_ms + int(seg.end * 1000),
                        "text": seg.text,
                    }
                )


def wait_until(condition: Callable[[], bool], timeout: float = 10) -> bool:
    """For tests: poll until `condition()` is true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False
