from __future__ import annotations

import numpy as np

from minutemind import audio_files, paths, store
from minutemind.diarize import Turn, assign_speakers
from minutemind.transcribe import RawSegment, TranscriptionQueue

from conftest import GPU_PROFILE, FakeDetector, FakeEngine


def seg(source, start, end, words=None, text=None, speaker=None):
    words = words or []
    return store.Segment(
        source=source,
        start_ms=start,
        end_ms=end,
        text=text if text is not None else "".join(w[2] for w in words).strip(),
        words=words,
        speaker=speaker,
    )


def test_lines_are_split_where_the_speaker_changes() -> None:
    line = seg(
        "system",
        0,
        4000,
        words=[(0, 500, " Thanks"), (600, 1200, " everyone."), (2100, 2600, " Sure,"), (2700, 3900, " sounds good.")],
    )
    turns = [Turn(0.0, 1.5, 7), Turn(2.0, 4.0, 3)]
    out = assign_speakers([line], turns)
    assert [(s.speaker, s.text, s.start_ms, s.end_ms) for s in out] == [
        ("S1", "Thanks everyone.", 0, 1200),
        ("S2", "Sure, sounds good.", 2100, 3900),
    ]


def test_mic_lines_are_left_alone_and_speakers_numbered_by_first_appearance() -> None:
    segments = [
        seg("mic", 0, 1000, text="Hi"),
        seg("system", 2000, 3000, words=[(2000, 3000, " Hello")]),
        seg("system", 4000, 5000, words=[(4000, 5000, " Morning")]),
        seg("system", 6000, 7000, words=[(6000, 7000, " Hello again")]),
    ]
    turns = [Turn(1.9, 3.1, 4), Turn(3.9, 5.1, 0), Turn(5.9, 7.1, 4)]
    out = assign_speakers(segments, turns)
    assert [(s.source, s.speaker, s.text) for s in out] == [
        ("mic", None, "Hi"),
        ("system", "S1", "Hello"),
        ("system", "S2", "Morning"),
        ("system", "S1", "Hello again"),
    ]


def test_lines_without_word_timings_use_overlap() -> None:
    out = assign_speakers(
        [seg("system", 1000, 5000, text="A long sentence")],
        [Turn(0.0, 2.0, 1), Turn(2.0, 6.0, 2)],
    )
    assert out[0].speaker == "S1"  # S1 is the first cluster seen (2), which overlaps most
    assert out[0].text == "A long sentence"


def test_words_in_a_gap_go_to_the_nearest_speaker() -> None:
    out = assign_speakers(
        [seg("system", 5000, 5400, words=[(5000, 5400, " um")])],
        [Turn(0.0, 2.0, 0), Turn(5.6, 8.0, 1)],
    )
    assert out[0].speaker == "S1"
    assert out[0].text == "um"


def test_no_turns_leaves_everything_unlabeled() -> None:
    line = seg("system", 0, 1000, words=[(0, 1000, " hi")])
    assert assign_speakers([line], []) == [line]


# -- in the transcription job -------------------------------------------------------


class TwoVoiceEngine(FakeEngine):
    """Returns one PC-sound line whose words switch speaker half-way."""

    def transcribe(self, audio, profile, on_progress):
        self.devices.append(profile.device)
        on_progress(len(audio) / audio_files.SAMPLE_RATE)
        if float(np.abs(audio).max()) * 32768 > 2000:
            words = [(0.1, 0.5, " The"), (0.5, 0.9, " quote"), (1.1, 1.5, " Agreed.")]
            return [RawSegment(0.1, 1.5, "The quote Agreed.", words)]
        return []


def _meeting(db: store.Store) -> int:
    meeting = db.create_meeting("Call", "Mic", "Speakers")
    folder = paths.meeting_dir(meeting.id)
    with audio_files.open_writer(folder / audio_files.SYSTEM_FILE) as w:
        w.writeframes(np.full(audio_files.SAMPLE_RATE * 2, 3000, np.int16).tobytes())
    db.finish_meeting(meeting.id, 2000)
    return meeting.id


def _run(db, engine, detector, job):
    q = TranscriptionQueue(db, engine, lambda: GPU_PROFILE, detector=detector)
    q.start()
    try:
        job(q)
        assert q.wait_idle(10)
    finally:
        q.stop()


def test_job_labels_two_speakers(db: store.Store) -> None:
    meeting_id = _meeting(db)
    detector = FakeDetector(turns=[Turn(0.0, 1.0, 5), Turn(1.0, 2.0, 2)])
    engine = TwoVoiceEngine()
    _run(db, engine, detector, lambda q: q.enqueue(meeting_id))

    meeting = db.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting.transcript_status == store.TRANSCRIPT_DONE
    assert meeting.speakers_status == store.SPEAKERS_DONE
    assert [(s.speaker, s.text) for s in db.get_segments(meeting_id)] == [
        ("S1", "The quote"),
        ("S2", "Agreed."),
    ]
    assert db.list_speakers(meeting_id) == [
        {"label": "S1", "display_name": "Speaker 1", "lines": 1},
        {"label": "S2", "display_name": "Speaker 2", "lines": 1},
    ]
    assert engine.releases >= 1  # Whisper unloaded before the speaker step


def test_detector_failure_keeps_the_transcript(db: store.Store) -> None:
    meeting_id = _meeting(db)
    _run(db, TwoVoiceEngine(), FakeDetector(error=RuntimeError("no models")), lambda q: q.enqueue(meeting_id))
    meeting = db.get_meeting(meeting_id)
    assert meeting is not None
    assert meeting.transcript_status == store.TRANSCRIPT_DONE
    assert meeting.speakers_status == store.SPEAKERS_FAILED
    assert meeting.speakers_error == "no models"
    assert [s.speaker for s in db.get_segments(meeting_id)] == [None]


def test_redetecting_speakers_resets_names_and_passes_the_count(db: store.Store) -> None:
    meeting_id = _meeting(db)
    detector = FakeDetector(turns=[Turn(0.0, 2.0, 0)])
    _run(db, TwoVoiceEngine(), detector, lambda q: q.enqueue(meeting_id))
    db.rename_speaker(meeting_id, "S1", "Priya")

    detector.turns = [Turn(0.0, 1.0, 0), Turn(1.0, 2.0, 1)]
    _run(db, TwoVoiceEngine(), detector, lambda q: q.enqueue_speakers(meeting_id, 2))
    assert detector.calls == [None, 2]
    assert [s["display_name"] for s in db.list_speakers(meeting_id)] == ["Speaker 1", "Speaker 2"]
    assert [s.speaker for s in db.get_segments(meeting_id)] == ["S1", "S2"]


def test_deleting_a_meeting_removes_its_speakers(db: store.Store) -> None:
    meeting_id = _meeting(db)
    db.set_speakers(meeting_id, ["S1"])
    db.delete_meeting(meeting_id)
    assert db.list_speakers(meeting_id) == []
