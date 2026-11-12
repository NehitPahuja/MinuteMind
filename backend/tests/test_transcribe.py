from __future__ import annotations

import numpy as np
import pytest

from minutemind import audio_files, hardware, paths, store
from minutemind.transcribe import RawSegment, TranscriptionQueue, merge_tracks

from conftest import GPU_PROFILE, OTHERS_SAID, YOU_SAID, FakeEngine


def _meeting_with_audio(db: store.Store, mic_level: int = 1000, system_level: int = 3000) -> int:
    meeting = db.create_meeting("Test", "Mic", "Speakers")
    folder = paths.meeting_dir(meeting.id)
    for name, level in ((audio_files.MIC_FILE, mic_level), (audio_files.SYSTEM_FILE, system_level)):
        with audio_files.open_writer(folder / name) as w:
            w.writeframes(np.full(audio_files.SAMPLE_RATE * 2, level, np.int16).tobytes())
    db.finish_meeting(meeting.id, 2000)
    return meeting.id


def _run(db: store.Store, engine: FakeEngine, meeting_id: int, profile=GPU_PROFILE) -> None:
    q = TranscriptionQueue(db, engine, lambda: profile)
    q.start()
    try:
        q.enqueue(meeting_id)
        assert q.wait_idle(10)
    finally:
        q.stop()


def test_transcribes_both_tracks_with_speaker_labels(db: store.Store) -> None:
    engine = FakeEngine()
    meeting_id = _meeting_with_audio(db)
    _run(db, engine, meeting_id)

    meeting = db.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting.transcript_status == store.TRANSCRIPT_DONE
    assert meeting.transcript_device == "GPU"
    segments = db.get_segments(meeting_id)
    assert [(s.source, s.text) for s in segments] == [("mic", YOU_SAID), ("system", OTHERS_SAID)]
    assert segments[0].start_ms == 100 and segments[0].end_ms == 1900
    assert segments[0].words == [(100, 400, "Good")]
    assert engine.releases == 1  # GPU memory freed once the queue went idle


def test_falls_back_to_cpu_when_the_gpu_fails(db: store.Store) -> None:
    engine = FakeEngine(fail_on_gpu=True)
    meeting_id = _meeting_with_audio(db)
    _run(db, engine, meeting_id)
    meeting = db.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting.transcript_status == store.TRANSCRIPT_DONE
    assert meeting.transcript_device == "CPU"
    assert engine.devices == ["cuda", "cpu", "cpu"]  # second track goes straight to CPU
    assert len(db.get_segments(meeting_id)) == 2


def test_other_errors_mark_the_transcript_failed(db: store.Store) -> None:
    engine = FakeEngine(error=ValueError("bad audio"))
    meeting_id = _meeting_with_audio(db)
    _run(db, engine, meeting_id)
    meeting = db.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting.transcript_status == store.TRANSCRIPT_FAILED
    assert meeting.transcript_error == "bad audio"


def test_silent_tracks_are_skipped(db: store.Store) -> None:
    engine = FakeEngine()
    meeting_id = _meeting_with_audio(db, mic_level=0)
    _run(db, engine, meeting_id)
    assert [s.source for s in db.get_segments(meeting_id)] == ["system"]
    assert len(engine.devices) == 1


def test_unfinished_jobs_resume_on_start(db: store.Store) -> None:
    meeting_id = _meeting_with_audio(db)
    db.set_transcript_status(meeting_id, store.TRANSCRIPT_RUNNING)  # app closed mid-job
    engine = FakeEngine()
    q = TranscriptionQueue(db, engine, lambda: GPU_PROFILE)
    q.start()
    try:
        assert q.wait_idle(10)
    finally:
        q.stop()
    assert db.get_meeting(meeting_id).transcript_status == store.TRANSCRIPT_DONE  # type: ignore[union-attr]


def test_enqueue_ignores_duplicates(db: store.Store) -> None:
    meeting_id = _meeting_with_audio(db)
    q = TranscriptionQueue(db, FakeEngine(), lambda: GPU_PROFILE)  # worker not started
    assert q.enqueue(meeting_id) is True
    assert q.enqueue(meeting_id) is False
    assert q.status() == {"current": None, "queued": [meeting_id]}
    assert db.get_meeting(meeting_id).transcript_status == store.TRANSCRIPT_QUEUED  # type: ignore[union-attr]


def test_merge_drops_speaker_bleed_but_keeps_real_mic_speech() -> None:
    others = [RawSegment(10.0, 14.0, "The hosting quote is four hundred and twenty dollars.", [])]
    mic = [
        # The call audio leaking from laptop speakers into the mic, picked up a bit late.
        RawSegment(10.3, 14.2, "the hosting quote is four hundred twenty dollars", []),
        # The user answering, overlapping in time but saying something else.
        RawSegment(13.5, 15.0, "Great, let's approve it.", []),
        RawSegment(2.0, 4.0, "Hi everyone.", []),
    ]
    merged = merge_tracks(mic, others)
    assert [(s.source, s.text) for s in merged] == [
        ("mic", "Hi everyone."),
        ("system", "The hosting quote is four hundred and twenty dollars."),
        ("mic", "Great, let's approve it."),
    ]


@pytest.mark.parametrize(
    ("vram", "cuda", "preference", "expected"),
    [
        (4096, True, "auto", "gpu"),
        (3072, True, "auto", "small_gpu"),
        (1024, True, "auto", "cpu"),
        (None, False, "auto", "cpu"),
        (4096, True, "cpu", "cpu"),
        (1024, True, "gpu", "gpu"),  # user insists
        (None, False, "gpu", "cpu"),  # but no CUDA at all
    ],
)
def test_choose_profile(vram, cuda, preference, expected) -> None:
    info = hardware.HardwareInfo(
        gpu_name="GPU" if cuda else None,
        vram_total_mb=vram,
        vram_free_mb=vram,
        cuda_available=cuda,
        ram_total_mb=16000,
        cpu_threads=8,
    )
    profile = hardware.choose_profile(info, preference)
    assert profile.name == expected
    assert profile.device == ("cpu" if expected == "cpu" else "cuda")
