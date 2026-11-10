from __future__ import annotations

import numpy as np
import pytest

from minutemind import audio_files, paths, store
from minutemind.session import ALL_OUTPUTS, RecordingError, RecordingSession

from conftest import LOOPBACK, MIC, FakeRecorder, fake_devices


@pytest.fixture
def session(db: store.Store) -> RecordingSession:
    return RecordingSession(db, recorder_factory=FakeRecorder, device_lister=fake_devices)


def test_record_pause_resume_stop(session: RecordingSession, db: store.Store) -> None:
    meeting = session.start("Planning", MIC.id, LOOPBACK.id)
    assert meeting.mic_device == "Test Mic"
    assert meeting.system_device == "Test Speakers"
    assert session.status()["state"] == store.RECORDING

    session.pause()
    assert session.status()["state"] == store.PAUSED
    assert db.get_meeting(meeting.id).status == store.PAUSED  # type: ignore[union-attr]
    session.resume()

    done = session.stop()
    assert done.status == store.COMPLETED
    assert done.duration_ms == 1000
    assert (paths.meeting_dir(meeting.id) / audio_files.MIX_FILE).exists()
    assert session.status()["state"] == "idle"


def test_default_title_when_blank(session: RecordingSession) -> None:
    meeting = session.start("   ", MIC.id, None)
    assert meeting.title.startswith("Meeting, ")


def test_only_one_recording_at_a_time(session: RecordingSession) -> None:
    session.start(None, MIC.id, None)
    with pytest.raises(RecordingError):
        session.start(None, MIC.id, None)


def test_needs_a_source(session: RecordingSession) -> None:
    with pytest.raises(RecordingError):
        session.start(None, None, None)


def test_unknown_device(session: RecordingSession) -> None:
    with pytest.raises(RecordingError):
        session.start(None, 999, None)


def test_actions_without_recording(session: RecordingSession) -> None:
    for action in (session.pause, session.resume, session.stop):
        with pytest.raises(RecordingError):
            action()


def test_recover_interrupted(session: RecordingSession, db: store.Store) -> None:
    # A meeting left 'recording' by a crash, with a track on disk.
    crashed = db.create_meeting("Crashed", "Mic", None)
    with audio_files.open_writer(paths.meeting_dir(crashed.id) / audio_files.MIC_FILE) as w:
        w.writeframes(np.zeros(audio_files.SAMPLE_RATE * 3, dtype=np.int16).tobytes())
    # And one with no audio at all.
    empty = db.create_meeting("Empty", "Mic", None)

    assert sorted(session.recover_interrupted()) == sorted([crashed.id, empty.id])
    recovered = db.get_meeting(crashed.id)
    assert recovered is not None
    assert recovered.status == store.RECOVERED
    assert recovered.duration_ms == 3000
    assert (paths.meeting_dir(crashed.id) / audio_files.MIX_FILE).exists()
    assert db.get_meeting(empty.id).status == store.FAILED  # type: ignore[union-attr]


def test_all_outputs_records_every_loopback(session: RecordingSession) -> None:
    meeting = session.start(None, MIC.id, ALL_OUTPUTS)
    assert meeting.system_device == "All outputs (Test Speakers, Test Headphones)"
    done = session.stop()
    folder = paths.meeting_dir(done.id)
    system = audio_files.read_samples(folder / audio_files.SYSTEM_FILE)
    assert system[0] == 2000  # both outputs' sound summed into one track
    assert not list(folder.glob(audio_files.SYSTEM_PART_GLOB))  # parts cleaned up


def test_failed_output_is_skipped_with_a_warning(
    session: RecordingSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeRecorder, "broken", {"Test Headphones"})
    meeting = session.start(None, MIC.id, ALL_OUTPUTS)
    assert meeting.system_device == "All outputs (Test Speakers)"
    assert any("Test Headphones" in e for e in session.status()["errors"])


def test_recover_interrupted_combines_output_parts(
    session: RecordingSession, db: store.Store
) -> None:
    crashed = db.create_meeting("Crashed", None, "All outputs (A, B)")
    folder = paths.meeting_dir(crashed.id)
    for index in range(2):
        with audio_files.open_writer(folder / audio_files.system_part_file(index)) as w:
            w.writeframes(np.full(audio_files.SAMPLE_RATE, 100, dtype=np.int16).tobytes())
    session.recover_interrupted()
    recovered = db.get_meeting(crashed.id)
    assert recovered is not None and recovered.status == store.RECOVERED
    assert recovered.has_system and recovered.duration_ms == 1000
    assert audio_files.read_samples(folder / audio_files.SYSTEM_FILE)[0] == 200
    assert not list(folder.glob(audio_files.SYSTEM_PART_GLOB))
