"""Recording the microphone and the PC's sound (WASAPI loopback) on Windows.

Each source is written to its own 16 kHz mono WAV. Audio arrives on PortAudio
callback threads; callbacks only copy the data into a queue, and one writer
thread per track does the resampling and disk writes.

Timeline alignment: WASAPI loopback delivers *nothing* while no sound is
playing, so a track could end up shorter than the meeting and drift out of
sync. Every block is therefore stamped with the active recording time (the
wall clock minus paused time), and the writer pads silence whenever a track
falls behind that clock.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import soxr

from minutemind import audio_files

log = logging.getLogger(__name__)

# A track is padded with silence only when it lags the clock by more than this,
# so ordinary callback jitter never inserts gaps.
DRIFT_TOLERANCE_S = 0.25

# PortAudio's initialise/terminate, device enumeration and stream open/close are
# not thread-safe: two API requests listing devices at the same moment crashed
# the whole process with an access violation. Every such call goes through this
# lock. (Audio callbacks and stream reads don't need it.)
PORTAUDIO_LOCK = threading.RLock()

# A level reading older than this counts as silence (loopback sends nothing when quiet).
LEVEL_STALE_S = 0.3


class ActiveClock:
    """Elapsed recording time that stops while paused."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._started: float | None = None
        self._paused_at: float | None = None
        self._paused_total = 0.0

    def start(self) -> None:
        self._started = self._now()

    def pause(self) -> None:
        if self._paused_at is None:
            self._paused_at = self._now()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += self._now() - self._paused_at
            self._paused_at = None

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def elapsed(self) -> float:
        if self._started is None:
            return 0.0
        end = self._paused_at if self._paused_at is not None else self._now()
        return max(0.0, end - self._started - self._paused_total)


class TrackWriter:
    """Downmixes, resamples to 16 kHz, keeps the track aligned to the clock,
    and writes it to a WAV file. Not thread-safe: use from one thread.

    Alignment is tracked on the *input* timeline: when a block arrives later
    than the samples received so far account for, silence is fed into the
    resampler first. Doing it before resampling keeps the resampler's own
    small delay out of the arithmetic.
    """

    def __init__(self, path: Path, in_rate: int) -> None:
        self.path = path
        self.in_rate = in_rate
        self._resampler = soxr.ResampleStream(
            in_rate, audio_files.SAMPLE_RATE, 1, dtype="float32", quality="HQ"
        )
        self._writer = audio_files.open_writer(path)
        self.frames_written = 0
        self._in_frames = 0  # input samples consumed, including padding

    def write(self, block: np.ndarray, at_s: float) -> None:
        """Write a float32 block (frames x channels) that ends at active time `at_s`."""
        mono = block.mean(axis=1) if block.ndim == 2 else block
        mono = mono.astype(np.float32, copy=False)
        expected_start = int(round(at_s * self.in_rate)) - len(mono)
        lag = expected_start - self._in_frames
        if lag > DRIFT_TOLERANCE_S * self.in_rate:
            self._resample(np.zeros(lag, dtype=np.float32))
        self._resample(mono)

    def finish(self, end_s: float) -> None:
        """Pad to the end of the recording, flush the resampler, and close."""
        target_in = int(round(end_s * self.in_rate))
        if target_in > self._in_frames:
            self._resample(np.zeros(target_in - self._in_frames, dtype=np.float32))
        tail = self._resampler.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
        # Trim or pad so every track of a meeting ends at exactly the same sample.
        target_out = int(round(end_s * audio_files.SAMPLE_RATE))
        room = max(0, target_out - self.frames_written)
        self._write_frames(tail[:room])
        if self.frames_written < target_out:
            self._write_frames(np.zeros(target_out - self.frames_written, dtype=np.float32))
        self._writer.close()

    def _resample(self, mono: np.ndarray) -> None:
        self._in_frames += len(mono)
        self._write_frames(self._resampler.resample_chunk(mono))

    def _write_frames(self, frames: np.ndarray) -> None:
        if len(frames) == 0:
            return
        pcm = np.clip(frames * 32767.0, -32768, 32767).astype(np.int16)
        self._writer.writeframes(pcm.tobytes())
        self.frames_written += len(pcm)


def rms_level(block: np.ndarray) -> float:
    """Loudness of a block as 0..1, on a dB scale so quiet speech still shows."""
    if block.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))
    if rms <= 1e-6:
        return 0.0
    db = 20 * np.log10(rms)  # -120 .. 0 dBFS
    return float(np.clip((db + 60) / 60, 0.0, 1.0))  # map -60..0 dBFS to 0..1


# ---------------------------------------------------------------------------
# Devices (pyaudiowpatch is imported lazily so tests run without audio hardware)


@dataclass(frozen=True)
class AudioDevice:
    id: int
    name: str
    channels: int
    rate: int
    is_default: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "channels": self.channels,
            "rate": self.rate,
            "is_default": self.is_default,
        }


def list_devices() -> dict[str, list[AudioDevice]]:
    """WASAPI microphones and loopback (PC sound) devices."""
    import pyaudiowpatch as pa

    with PORTAUDIO_LOCK:
        return _list_devices_locked(pa)


def _list_devices_locked(pa) -> dict[str, list[AudioDevice]]:
    p = pa.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        default_in = wasapi["defaultInputDevice"]
        try:
            default_loopback = p.get_default_wasapi_loopback()["index"]
        except (OSError, LookupError):
            default_loopback = -1

        inputs: list[AudioDevice] = []
        loopbacks: list[AudioDevice] = []
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info["hostApi"] != wasapi["index"] or info["maxInputChannels"] < 1:
                continue
            is_loopback = bool(info.get("isLoopbackDevice"))
            device = AudioDevice(
                id=i,
                name=str(info["name"]).removesuffix(" [Loopback]"),
                channels=int(info["maxInputChannels"]),
                rate=int(info["defaultSampleRate"]),
                is_default=i == (default_loopback if is_loopback else default_in),
            )
            (loopbacks if is_loopback else inputs).append(device)
        return {"inputs": inputs, "loopbacks": loopbacks}
    finally:
        p.terminate()


# ---------------------------------------------------------------------------
# Recorder


@dataclass
class _Track:
    kind: str  # "mic" | "system"
    device: AudioDevice
    writer: TrackWriter
    queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=2000))
    level: float = 0.0
    level_at: float = 0.0
    stream: object | None = None
    thread: threading.Thread | None = None
    error: str | None = None


class Recorder:
    """Records one meeting: a mic track and/or a PC-sound track.

    PC sound can come from several output devices at once (each one's
    loopback); they are combined into a single "system" track at the end, so
    sound is captured whichever output an app happens to play through.
    """

    def __init__(
        self,
        directory: Path,
        mic: AudioDevice | None,
        system: list[AudioDevice],
    ):
        if mic is None and not system:
            raise ValueError("Choose at least one source to record")
        self.directory = directory
        self.clock = ActiveClock()
        self._pa = None
        self._tracks: list[_Track] = []
        self._mic = mic
        self._system = list(system)
        self.warnings: list[str] = []

    @property
    def has_mic(self) -> bool:
        return any(t.kind == "mic" for t in self._tracks)

    @property
    def system_devices(self) -> list[AudioDevice]:
        """The output devices actually being recorded (some may have failed to open)."""
        return [t.device for t in self._tracks if t.kind == "system"]

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with PORTAUDIO_LOCK:
            self._start_locked()

    def _start_locked(self) -> None:
        self._pa = self._open_portaudio()
        self.clock.start()
        try:
            if self._mic is not None:
                # The user's own voice is essential: if the mic can't open, fail.
                self._open_track("mic", self._mic, audio_files.MIC_FILE)
            for index, device in enumerate(self._system):
                try:
                    self._open_track("system", device, audio_files.system_part_file(index))
                except Exception as exc:  # noqa: BLE001 - other outputs can still record
                    self.warnings.append(f"Couldn't record {device.name}: {exc}")
                    log.warning("Skipping loopback %s: %s", device.name, exc)
            if not self._tracks:
                raise RuntimeError("None of the chosen audio devices could be opened")
        except Exception:
            self.stop()
            raise

    def pause(self) -> None:
        self.clock.pause()

    def resume(self) -> None:
        self.clock.resume()

    def stop(self) -> int:
        """Stop all streams, finish the files and build the mix. Returns duration in ms."""
        end_s = self.clock.elapsed()
        with PORTAUDIO_LOCK:
            for track in self._tracks:
                stream = track.stream
                track.stream = None
                if stream is not None:
                    try:
                        stream.stop_stream()  # type: ignore[attr-defined]
                        stream.close()  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001 - best effort during shutdown
                        log.exception("Closing %s stream failed", track.device.name)
        for track in self._tracks:
            track.queue.put(None)  # sentinel
            if track.thread is not None:
                track.thread.join(timeout=10)
            track.writer.finish(end_s)
        with PORTAUDIO_LOCK:
            if self._pa is not None:
                self._pa.terminate()
                self._pa = None
        audio_files.combine_system_parts(self.directory)
        audio_files.mix_tracks(self.directory)
        return int(end_s * 1000)

    # -- status ------------------------------------------------------------

    def levels(self) -> dict[str, float]:
        """Current loudness per kind; for PC sound, the loudest output device."""
        result = {"mic": 0.0, "system": 0.0}
        if self.clock.paused:
            return result
        now = time.monotonic()
        for track in self._tracks:
            if now - track.level_at < LEVEL_STALE_S:
                result[track.kind] = max(result[track.kind], track.level)
        return result

    def errors(self) -> list[str]:
        return self.warnings + [f"{t.device.name}: {t.error}" for t in self._tracks if t.error]

    # -- internals ---------------------------------------------------------

    def _open_portaudio(self):
        import pyaudiowpatch as pa

        return pa.PyAudio()

    def _open_track(self, kind: str, device: AudioDevice, filename: str) -> None:
        import pyaudiowpatch as pa

        track = _Track(kind, device, TrackWriter(self.directory / filename, device.rate))
        try:
            track.stream = self._pa.open(  # type: ignore[union-attr]
                format=pa.paFloat32,
                channels=device.channels,
                rate=device.rate,
                input=True,
                input_device_index=device.id,
                frames_per_buffer=device.rate // 20,  # 50 ms
                stream_callback=self._make_callback(track),
            )
        except Exception:
            track.writer.finish(0)
            (self.directory / filename).unlink(missing_ok=True)
            raise
        track.thread = threading.Thread(
            target=self._drain, args=(track,), name=f"writer-{filename}", daemon=True
        )
        track.thread.start()
        self._tracks.append(track)

    def _make_callback(self, track: _Track):
        import pyaudiowpatch as pa

        channels = track.device.channels

        def callback(in_data, frame_count, _time_info, _status):
            if self.clock.paused:
                return (None, pa.paContinue)
            block = np.frombuffer(in_data, dtype=np.float32).reshape(-1, channels).copy()
            track.level = rms_level(block)
            track.level_at = time.monotonic()
            try:
                track.queue.put_nowait((block, self.clock.elapsed()))
            except queue.Full:
                track.error = "audio writer fell behind; some audio was dropped"
            return (None, pa.paContinue)

        return callback

    def _drain(self, track: _Track) -> None:
        while True:
            item = track.queue.get()
            if item is None:
                return
            block, at_s = item
            try:
                track.writer.write(block, at_s)
            except Exception as exc:  # noqa: BLE001 - keep recording the other tracks
                track.error = str(exc)
                log.exception("Writing %s track failed", track.device.name)
