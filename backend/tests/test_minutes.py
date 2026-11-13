from __future__ import annotations

import json

import numpy as np
import pytest

from minutemind import audio_files, hardware, minutes, paths, store
from minutemind.llm.llamacpp import SMALL_GPU_LAYERS, gpu_layers_for
from minutemind.minutes import Line, MeetingContext, MinutesWriter, RawMinutes
from minutemind.transcribe import TranscriptionQueue

from conftest import FAKE_MINUTES, GPU_PROFILE, FakeEngine, FakeLanguageModel


def _ctx(lines: list[Line], duration_ms: int = 600_000, notes=None) -> MeetingContext:
    return MeetingContext(
        title="Budget sync",
        created_at="2026-03-14T10:00:00+00:00",
        duration_ms=duration_ms,
        lines=lines,
        notes=notes or [],
        people=sorted({line.who for line in lines}),
    )


# -- formatting --------------------------------------------------------------------


def test_clock_round_trip() -> None:
    assert minutes.clock(65_000) == "01:05"
    assert minutes.clock(3_725_000) == "1:02:05"
    assert minutes.parse_clock("01:05") == 65_000
    assert minutes.parse_clock("[1:02:05]") == 3_725_000
    assert minutes.parse_clock("12:75") is None
    assert minutes.parse_clock("soon") is None


def test_join_lines_merges_one_persons_back_to_back_lines() -> None:
    lines = [
        Line(0, 2000, "You", "Hello."),
        Line(2500, 4000, "You", "Let's start."),
        Line(4500, 6000, "Priya", "Sure."),
        Line(60_000, 61_000, "Priya", "Much later."),
    ]
    joined = minutes.join_lines(lines)
    assert [(l.who, l.text, l.start_ms, l.end_ms) for l in joined] == [
        ("You", "Hello. Let's start.", 0, 4000),
        ("Priya", "Sure.", 4500, 6000),
        ("Priya", "Much later.", 60_000, 61_000),
    ]


def test_build_context_uses_speaker_names(db: store.Store) -> None:
    meeting = db.create_meeting("Call", "Mic", "Speakers")
    db.replace_segments(
        meeting.id,
        [
            store.Segment("mic", 0, 1000, "Hi all.", []),
            store.Segment("system", 1500, 2500, "Hello.", [], speaker="S1"),
            store.Segment("system", 40_000, 41_000, "Bye.", [], speaker="S2"),
            store.Segment("system", 90_000, 91_000, "Unlabeled.", []),
        ],
    )
    db.set_speakers(meeting.id, ["S1", "S2"])
    db.rename_speaker(meeting.id, "S1", "Priya")
    db.add_note(meeting.id, 30_000, "Ask about the budget")

    ctx = minutes.build_context(db, meeting.id)
    assert [l.who for l in ctx.lines] == ["You", "Priya", "Speaker 2", "Others"]
    assert ctx.people == ["You", "Priya", "Speaker 2", "Others"]
    assert ctx.notes == [(30_000, "Ask about the budget")]


# -- writing -----------------------------------------------------------------------


def test_writes_minutes_in_one_request_and_tidies_them() -> None:
    answer = {
        "summary": "  Budget agreed.  ",
        "decisions": ["Hosting is $420 per month.", "hosting is $420 per month", ""],
        "action_items": [
            {"task": "Email Priya the quote", "owner": "You", "due": "14 March"},
            {"task": "Email Priya the quote.", "owner": "You", "due": ""},
            {"task": "Book a room", "owner": "N/A", "due": "none"},
        ],
        "topics": [
            {"title": "Hosting", "at": "00:05"},
            {"title": "Imaginary", "at": "99:00"},
            {"title": "No time", "at": ""},
        ],
    }
    llm = FakeLanguageModel(answers=[json.dumps(answer)])
    ctx = _ctx(
        [Line(5000, 9000, "Priya", "Hosting is $420 per month."), Line(9500, 12_000, "You", "Agreed.")],
        notes=[(7000, "check the contract")],
    )
    progress: list[float] = []
    result = MinutesWriter(llm).write(ctx, GPU_PROFILE, progress.append)

    assert len(llm.prompts) == 1
    prompt = llm.prompts[0]
    assert "Meeting: Budget sync" in prompt
    assert "[00:05] Priya: Hosting is $420 per month." in prompt
    assert "[00:07] check the contract" in prompt
    assert "People speaking: Priya, You" in prompt
    assert result == {
        "summary": "Budget agreed.",
        "decisions": ["Hosting is $420 per month."],
        "action_items": [
            {"task": "Email Priya the quote", "owner": "You", "due": "14 March"},
            {"task": "Book a room", "owner": None, "due": None},
        ],
        "topics": [
            {"title": "Hosting", "start_ms": 5000},
            {"title": "Imaginary", "start_ms": None},
            {"title": "No time", "start_ms": None},
        ],
    }
    assert progress[0] == 0.0 and progress[-1] == 1.0


def test_retries_once_when_the_answer_is_not_valid() -> None:
    llm = FakeLanguageModel(answers=['{"summary": "cut off', json.dumps(FAKE_MINUTES)])
    result = MinutesWriter(llm).write(_ctx([Line(0, 1000, "You", "Hi.")]), GPU_PROFILE)
    assert len(llm.prompts) == 2
    assert result["decisions"] == FAKE_MINUTES["decisions"]

    always_bad = FakeLanguageModel(answers=['{"summary": 3}'])
    with pytest.raises(ValueError, match="valid minutes"):
        MinutesWriter(always_bad).write(_ctx([Line(0, 1000, "You", "Hi.")]), GPU_PROFILE)


def test_empty_transcript_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        MinutesWriter(FakeLanguageModel()).write(_ctx([]), GPU_PROFILE)


def test_long_meetings_are_split_then_combined() -> None:
    part = lambda n: json.dumps(  # noqa: E731
        {
            "summary": f"Part {n}.",
            "decisions": [f"Decision {n}"],
            "action_items": [],
            "topics": [{"title": f"Topic {n}", "at": "00:00"}],
        }
    )
    combined = json.dumps(
        {
            "summary": "Whole meeting.",
            "decisions": ["Decision 1", "Decision 2", "Decision 3"],
            "action_items": [],
            "topics": [],
        }
    )
    # A tiny context forces the transcript into several parts.
    llm = FakeLanguageModel(answers=[part(1), part(2), part(3), combined], context_tokens=3200)
    writer = MinutesWriter(llm)
    budget = writer.input_budget_chars()
    sentence = "word " * (budget // 12)
    lines = [Line(i * 60_000, i * 60_000 + 5000, "You" if i % 2 else "Priya", sentence) for i in range(5)]
    parts = minutes.split_lines(lines, budget)
    assert len(parts) == 3

    result = writer.write(_ctx(lines), GPU_PROFILE)
    assert len(llm.prompts) == 4
    assert "(part 1 of 3)" in llm.prompts[0] and "(part 3 of 3)" in llm.prompts[2]
    assert "consecutive parts of one meeting" in llm.prompts[3]
    assert '"Decision 2"' in llm.prompts[3]
    assert result["summary"] == "Whole meeting."
    assert result["decisions"] == ["Decision 1", "Decision 2", "Decision 3"]


def test_notes_go_with_the_part_they_belong_to() -> None:
    llm = FakeLanguageModel(context_tokens=3200)
    writer = MinutesWriter(llm)
    sentence = "word " * (writer.input_budget_chars() // 6)
    lines = [Line(0, 1000, "You", sentence), Line(600_000, 601_000, "You", sentence)]
    writer.write(_ctx(lines, notes=[(500, "early note"), (600_500, "late note")]), GPU_PROFILE)
    assert "early note" in llm.prompts[0] and "late note" not in llm.prompts[0]
    assert "late note" in llm.prompts[1] and "early note" not in llm.prompts[1]


def test_partials_too_big_to_combine_are_merged_without_the_model() -> None:
    big = RawMinutes(summary="x" * 50, decisions=["A"], action_items=[], topics=[])
    other = RawMinutes(summary="y" * 50, decisions=["B"], action_items=[], topics=[])
    assert len(minutes.group_partials([big, other], budget_chars=60)) == 2
    merged = minutes.merge_mechanically([big, other])
    assert merged.decisions == ["A", "B"]


def test_markdown_export(db: store.Store) -> None:
    meeting = db.create_meeting("Budget sync", "Mic", None)
    db.finish_meeting(meeting.id, 754_000)
    meeting = db.get_meeting(meeting.id)
    md = minutes.to_markdown(
        meeting,
        {
            "summary": "We agreed the budget.",
            "decisions": ["Hosting is $420 per month."],
            "action_items": [
                {"task": "Email the quote", "owner": "Priya", "due": "Friday"},
                {"task": "Book a room", "owner": None, "due": None},
            ],
            "topics": [{"title": "Hosting", "start_ms": 65_000}, {"title": "Misc", "start_ms": None}],
        },
    )
    assert md.startswith("# Budget sync\n")
    assert "· 12:34" in md
    assert "## Summary\n\nWe agreed the budget." in md
    assert "- [ ] Email the quote (**Priya**, due Friday)" in md
    assert "- [ ] Book a room\n" in md
    assert "- `01:05` Hosting" in md and "- Misc" in md

    empty = minutes.to_markdown(meeting, {"summary": "", "decisions": [], "action_items": [], "topics": []})
    assert "_No decisions were recorded._" in empty and "## Topics" not in empty


def test_gpu_layers_follow_the_hardware_profile() -> None:
    assert gpu_layers_for(GPU_PROFILE) == -1
    small = hardware.Profile("small_gpu", "cuda", "int8_float16", "Small GPU")
    assert gpu_layers_for(small) == SMALL_GPU_LAYERS
    assert gpu_layers_for(hardware.CPU_PROFILE) == 0


# -- the queue ---------------------------------------------------------------------


def _meeting_with_audio(db: store.Store) -> int:
    meeting = db.create_meeting("Test", "Mic", None)
    folder = paths.meeting_dir(meeting.id)
    with audio_files.open_writer(folder / audio_files.MIC_FILE) as w:
        w.writeframes(np.full(audio_files.SAMPLE_RATE * 2, 1000, np.int16).tobytes())
    db.finish_meeting(meeting.id, 2000)
    return meeting.id


class OrderedEngine(FakeEngine):
    """Logs when Whisper runs and is released, to check the GPU hand-off."""

    def __init__(self, log: list) -> None:
        super().__init__()
        self.log = log

    def transcribe(self, audio, profile, on_progress):
        self.log.append(("whisper", profile.device))
        return super().transcribe(audio, profile, on_progress)

    def release(self) -> None:
        self.log.append(("release-whisper", None))
        super().release()


def _queue(db, engine, llm, enabled=True) -> TranscriptionQueue:
    return TranscriptionQueue(
        db,
        engine,
        lambda: GPU_PROFILE,
        writer=MinutesWriter(llm),
        minutes_enabled=lambda: enabled,
    )


def _run(q: TranscriptionQueue, job) -> None:
    q.start()
    try:
        job(q)
        assert q.wait_idle(10)
    finally:
        q.stop()


def test_full_job_releases_whisper_before_writing_minutes(db: store.Store) -> None:
    log: list = []
    meeting_id = _meeting_with_audio(db)
    llm = FakeLanguageModel(log=log)
    _run(_queue(db, OrderedEngine(log), llm), lambda q: q.enqueue(meeting_id))

    meeting = db.get_meeting(meeting_id)
    assert meeting.transcript_status == store.TRANSCRIPT_DONE
    assert meeting.minutes_status == store.MINUTES_DONE
    saved = db.get_minutes(meeting_id)
    assert saved["model"] == "fake-llm" and saved["device"] == "GPU"
    first_llm = log.index(("llm", "cuda"))
    assert ("release-whisper", None) in log[log.index(("whisper", "cuda")) : first_llm]
    assert llm.releases >= 1  # freed again once idle


def test_minutes_fall_back_to_the_cpu(db: store.Store) -> None:
    meeting_id = _meeting_with_audio(db)
    llm = FakeLanguageModel(fail_on_gpu=True)
    _run(_queue(db, FakeEngine(), llm), lambda q: q.enqueue(meeting_id))
    assert llm.devices == ["cuda", "cpu"]
    assert db.get_minutes(meeting_id)["device"] == "CPU"


def test_minutes_off_skips_the_model(db: store.Store) -> None:
    meeting_id = _meeting_with_audio(db)
    llm = FakeLanguageModel()
    _run(_queue(db, FakeEngine(), llm, enabled=False), lambda q: q.enqueue(meeting_id))
    assert llm.prompts == [] and llm.downloads == 0
    assert db.get_meeting(meeting_id).minutes_status == store.MINUTES_NONE


def test_regenerate_and_resume_unfinished_minutes(db: store.Store) -> None:
    meeting_id = _meeting_with_audio(db)
    db.replace_segments(meeting_id, [store.Segment("mic", 0, 1000, "Hi.", [])])
    db.set_transcript_status(meeting_id, store.TRANSCRIPT_DONE)
    # Left "running" by an app that closed mid-way: picked up again at start.
    db.set_minutes_status(meeting_id, store.MINUTES_RUNNING)
    llm = FakeLanguageModel()
    _run(_queue(db, FakeEngine(), llm), lambda q: None)
    assert db.get_meeting(meeting_id).minutes_status == store.MINUTES_DONE
    assert len(llm.prompts) == 1

    llm.answers = [json.dumps({**FAKE_MINUTES, "summary": "Second try."})]
    _run(_queue(db, FakeEngine(), llm), lambda q: q.enqueue_minutes(meeting_id))
    assert db.get_minutes(meeting_id)["content"]["summary"] == "Second try."


def test_meeting_with_nothing_said_gets_no_minutes(db: store.Store) -> None:
    meeting_id = _meeting_with_audio(db)
    db.set_transcript_status(meeting_id, store.TRANSCRIPT_DONE)
    llm = FakeLanguageModel()
    _run(_queue(db, FakeEngine(), llm), lambda q: q.enqueue_minutes(meeting_id))
    assert llm.prompts == []
    assert db.get_meeting(meeting_id).minutes_status == store.MINUTES_NONE


# -- storage -----------------------------------------------------------------------


def test_minutes_are_stored_replaced_and_deleted_with_the_meeting(db: store.Store) -> None:
    meeting = db.create_meeting("Call", "Mic", None)
    assert db.get_minutes(meeting.id) is None
    db.set_minutes_status(meeting.id, store.MINUTES_FAILED, "boom")
    db.save_minutes(meeting.id, {"summary": "One"}, "m", "GPU")
    db.save_minutes(meeting.id, {"summary": "Two"}, "m", "CPU")
    saved = db.get_minutes(meeting.id)
    assert saved["content"] == {"summary": "Two"} and saved["device"] == "CPU"
    after = db.get_meeting(meeting.id)
    assert after.minutes_status == store.MINUTES_DONE and after.minutes_error is None
    db.delete_meeting(meeting.id)
    assert db.get_minutes(meeting.id) is None


def test_language_model_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    from minutemind import models

    assert models.minutes_llm() is models.MINUTES_LLM
    monkeypatch.setenv("MINUTEMIND_LLM", "qwen2.5-0.5b-instruct")
    assert models.minutes_llm() is models.SMALL_LLM
    monkeypatch.setenv("MINUTEMIND_LLM", "nonsense")
    assert models.minutes_llm() is models.MINUTES_LLM


def test_llamacpp_model_uses_the_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from minutemind import models
    from minutemind.llm.llamacpp import LlamaCppModel

    default = LlamaCppModel(models.ModelManager())
    assert default.name == models.MINUTES_LLM.name
    assert default.context_tokens == models.MINUTES_LLM.context_tokens
    monkeypatch.setenv("MINUTEMIND_LLM", "qwen2.5-0.5b-instruct")
    assert LlamaCppModel(models.ModelManager()).spec is models.SMALL_LLM
