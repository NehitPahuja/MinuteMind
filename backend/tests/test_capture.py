from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from minutemind import audio_files
from minutemind.capture import ActiveClock, TrackWriter, rms_level


class FakeTime:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_clock_excludes_paused_time() -> None:
    now = FakeTime()
    clock = ActiveClock(now)
    clock.start()
    now.t += 10
    clock.pause()
    now.t += 5
    assert clock.elapsed() == pytest.approx(10)
    clock.resume()
    now.t += 2
    assert clock.elapsed() == pytest.approx(12)
    assert not clock.paused


def _sine(rate: int, seconds: float, channels: int) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    mono = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return np.repeat(mono[:, None], channels, axis=1)


def _feed(writer: TrackWriter, audio: np.ndarray, rate: int, start_s: float) -> float:
    """Feed audio in 50 ms blocks stamped with their end time; return the end time."""
    block = rate // 20
    at = start_s
    for i in range(0, len(audio), block):
        chunk = audio[i : i + block]
        at += len(chunk) / rate
        writer.write(chunk, at)
    return at


def test_writer_resamples_and_downmixes(tmp_path: Path) -> None:
    path = tmp_path / "t.wav"
    writer = TrackWriter(path, 48_000)
    end = _feed(writer, _sine(48_000, 2.0, 2), 48_000, 0.0)
    writer.finish(end)
    samples = audio_files.read_samples(path)
    assert len(samples) == pytest.approx(2 * audio_files.SAMPLE_RATE, abs=160)
    assert np.abs(samples).max() > 5000  # the tone survived


def test_writer_pads_silence_when_source_goes_quiet(tmp_path: Path) -> None:
    """Loopback sends nothing while nothing plays; the track must stay on the clock."""
    path = tmp_path / "t.wav"
    writer = TrackWriter(path, 44_100)
    _feed(writer, _sine(44_100, 1.0, 2), 44_100, 0.0)  # 0-1 s: sound
    end = _feed(writer, _sine(44_100, 1.0, 2), 44_100, 4.0)  # 1-4 s: nothing, 4-5 s: sound
    writer.finish(end)
    samples = audio_files.read_samples(path)
    rate = audio_files.SAMPLE_RATE
    assert len(samples) == pytest.approx(5 * rate, abs=320)
    assert np.abs(samples[int(2 * rate) : int(3.5 * rate)]).max() == 0  # the gap is silent
    assert np.abs(samples[int(4.2 * rate) : int(4.8 * rate)]).max() > 5000  # 2nd tone in place


def test_finish_pads_to_end_of_recording(tmp_path: Path) -> None:
    path = tmp_path / "t.wav"
    writer = TrackWriter(path, 48_000)
    _feed(writer, _sine(48_000, 1.0, 1), 48_000, 0.0)
    writer.finish(3.0)  # recording ran 3 s but the source stopped after 1 s
    assert len(audio_files.read_samples(path)) == pytest.approx(3 * audio_files.SAMPLE_RATE, abs=1)


def test_rms_level_range() -> None:
    assert rms_level(np.zeros(100, dtype=np.float32)) == 0.0
    assert rms_level(np.ones(100, dtype=np.float32)) == pytest.approx(1.0)
    quiet = rms_level(np.full(100, 0.01, dtype=np.float32))  # -40 dBFS
    assert 0.2 < quiet < 0.5
