from __future__ import annotations

import numpy as np
import pytest

from minutemind import audio_files, hardware
from minutemind.live import (
    LiveBuffer,
    LiveTranscriber,
    MARGIN_S,
    choose_cut,
    live_model_for,
    wait_until,
)

from conftest import GPU_PROFILE, LIVE_OTHERS, LIVE_YOU, FakeLiveEngine

SR = audio_files.SAMPLE_RATE


def tone(seconds: float, level: float = 0.1) -> np.ndarray:
    return np.full(int(seconds * SR), level, np.float32)


# -- LiveBuffer -------------------------------------------------------------------


def test_buffer_sums_overlapping_writers() -> None:
    buf = LiveBuffer()
    buf.add(0, np.full(100, 0.1, np.float32))  # speakers
    buf.add(50, np.full(100, 0.2, np.float32))  # headphones, overlapping
    data = buf.read(0, 150)
    assert data[10] == pytest.approx(0.1)
    assert data[60] == pytest.approx(0.3)
    assert data[120] == pytest.approx(0.2)
    assert buf.newest == 150


def test_buffer_holds_back_a_safety_margin() -> None:
    buf = LiveBuffer()
    buf.add(0, tone(2.0))
    assert buf.available() == int(2.0 * SR) - int(MARGIN_S * SR)


def test_buffer_forgets_transcribed_audio() -> None:
    buf = LiveBuffer()
    buf.add(0, tone(3.0))
    buf.advance(SR)
    assert buf.processed == SR
    assert len(buf.read(0, SR)) == 0  # dropped
    assert len(buf.read(SR, 2 * SR)) == SR
    buf.add(0, tone(0.5, 0.9))  # late audio for an already-transcribed stretch
    assert float(buf.read(SR, SR + 10).max()) == pytest.approx(0.1)


# -- cutting slices -----------------------------------------------------------------


def test_cut_lands_in_the_pause() -> None:
    audio = tone(10.0, 0.2)
    pause_start = int(8.4 * SR)
    audio[pause_start : pause_start + int(0.4 * SR)] = 0.0  # a short pause near the end
    cut = choose_cut(audio)
    assert pause_start <= cut <= pause_start + int(0.4 * SR)


def test_short_audio_is_not_cut() -> None:
    audio = tone(2.0)
    assert choose_cut(audio) == len(audio)


def test_live_model_choice() -> None:
    assert live_model_for(GPU_PROFILE) == "small"
    assert live_model_for(hardware.CPU_PROFILE) == "base"


# -- LiveTranscriber ----------------------------------------------------------------


def make(engine: FakeLiveEngine, **kwargs) -> LiveTranscriber:
    return LiveTranscriber(engine, lambda: GPU_PROFILE, chunk_s=1.0, **kwargs)


def test_transcribes_both_speakers_with_meeting_times() -> None:
    engine = FakeLiveEngine()
    live = make(engine)
    live.start(meeting_id=7)
    try:
        live.feed("mic", 0, tone(2.5, 0.1))
        live.feed("system", int(5 * SR), tone(2.5, 0.6))  # the call speaks from 5 s
        assert wait_until(lambda: live.snapshot()["count"] >= 2)
    finally:
        live.stop()
    lines = live.snapshot()["lines"]
    by_source = {line["source"]: line for line in lines}
    assert by_source["mic"]["text"] == LIVE_YOU
    assert by_source["system"]["text"] == LIVE_OTHERS
    assert by_source["system"]["start_ms"] >= 5000  # placed on the meeting timeline
    assert [line["id"] for line in lines] == list(range(len(lines)))
    assert live.snapshot()["meeting_id"] == 7
    assert live.snapshot()["state"] == "off"
    assert engine.releases == 1


def test_snapshot_since_returns_only_new_lines() -> None:
    engine = FakeLiveEngine()
    live = make(engine)
    live.start(1)
    try:
        live.feed("mic", 0, tone(1.5))
        assert wait_until(lambda: live.snapshot()["count"] >= 1)
        first = live.snapshot()["count"]
        live.feed("mic", int(1.5 * SR), tone(1.5))
        assert wait_until(lambda: live.snapshot()["count"] > first)
        assert len(live.snapshot(first)["lines"]) == live.snapshot()["count"] - first
    finally:
        live.stop()


def test_silence_is_skipped_without_transcribing() -> None:
    engine = FakeLiveEngine()
    live = make(engine)
    live.start(1)
    try:
        live.feed("mic", 0, tone(3.0, 0.0))
        assert wait_until(lambda: live._buffers["mic"].processed > 0)
    finally:
        live.stop()
    assert engine.calls == []
    assert live.snapshot()["count"] == 0


def test_waits_while_the_gpu_is_busy() -> None:
    engine = FakeLiveEngine()
    busy = [True]
    live = make(engine, gpu_free=lambda: not busy[0])
    live.start(1)
    try:
        live.feed("mic", 0, tone(2.0))
        assert wait_until(lambda: live.snapshot()["state"] == "waiting")
        assert engine.prepared == []
        busy[0] = False  # the final transcript finished
        assert wait_until(lambda: live.snapshot()["count"] >= 1)
    finally:
        live.stop()


def test_falls_back_to_the_cpu_model() -> None:
    engine = FakeLiveEngine(fail_on_gpu=True)
    live = make(engine)
    live.start(1)
    try:
        live.feed("mic", 0, tone(2.0))
        assert wait_until(lambda: live.snapshot()["count"] >= 1)
    finally:
        live.stop()
    assert engine.calls[0][:2] == ("small", "cuda")
    assert engine.calls[-1][:2] == ("base", "cpu")
    assert engine.prepared == ["small", "base"]


def test_restarting_clears_the_previous_meeting() -> None:
    engine = FakeLiveEngine()
    live = make(engine)
    live.start(1)
    live.feed("mic", 0, tone(1.5))
    assert wait_until(lambda: live.snapshot()["count"] >= 1)
    live.start(2)
    try:
        assert live.snapshot()["count"] == 0
        assert live.snapshot()["meeting_id"] == 2
    finally:
        live.stop()
