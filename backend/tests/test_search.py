from __future__ import annotations

import json
import threading
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from minutemind import index, models, qa, store
from minutemind.api import TOKEN_HEADER, create_app
from minutemind.index import Indexer, Item
from minutemind.session import RecordingSession

from conftest import (
    GPU_PROFILE,
    MIC,
    FakeDetector,
    FakeEmbedder,
    FakeEngine,
    FakeHardwareCache,
    FakeLanguageModel,
    FakeLiveEngine,
    FakeRecorder,
    fake_devices,
)

AUTH = {TOKEN_HEADER: "test-token"}

LAUNCH_LINES = [
    ("mic", None, 500, 8000, "Let us decide the launch date for the mobile app."),
    ("system", "S1", 9500, 16000, "I think we should launch on the first of December."),
    ("system", "S2", 17500, 25000, "The onboarding videos are not ready, I can finish them by Friday."),
    ("system", "S1", 45000, 49000, "I will send the beta invite to the 50 testers by Wednesday."),
    ("mic", None, 50000, 55000, "The hosting quote came in at $420 per month."),
]


def _transcribed_meeting(db: store.Store, title: str = "Launch sync", lines=LAUNCH_LINES) -> int:
    meeting = db.create_meeting(title, "Mic", "Speakers")
    db.replace_segments(
        meeting.id,
        [store.Segment(src, start, end, text, [], speaker=spk) for src, spk, start, end, text in lines],
    )
    labels = sorted({spk for _, spk, *_ in lines if spk})
    db.set_speakers(meeting.id, labels)
    db.finish_meeting(meeting.id, max(end for *_, end, _ in lines) + 1000)
    db.set_transcript_status(meeting.id, store.TRANSCRIPT_DONE)
    return meeting.id


def _indexed(db: store.Store, embedder=None) -> tuple[Indexer, int]:
    meeting_id = _transcribed_meeting(db)
    db.rename_speaker(meeting_id, "S2", "Hazel")
    indexer = Indexer(db, embedder if embedder is not None else FakeEmbedder())
    indexer.index_meeting(meeting_id)
    return indexer, meeting_id


# -- passages ----------------------------------------------------------------------------


def test_passages_are_about_45_seconds_and_overlap() -> None:
    items = [Item(t * 10_000, t * 10_000 + 8_000, f"Line {t}") for t in range(12)]
    passages = index.cut_passages(items)
    assert len(passages) > 1
    for p in passages:
        assert p.end_ms - p.start_ms <= index.WINDOW_MS
    # Each passage after the first starts with the tail of the previous one.
    for prev, nxt in zip(passages, passages[1:]):
        assert nxt.text.split("\n")[0] in prev.text
    # Nothing is lost.
    joined = "\n".join(p.text for p in passages)
    assert all(f"Line {t}" in joined for t in range(12))


def test_long_text_starts_a_new_passage() -> None:
    items = [Item(i * 1000, i * 1000 + 900, "x" * 500) for i in range(5)]
    passages = index.cut_passages(items)
    assert all(len(p.text) <= index.MAX_CHARS + 600 for p in passages)
    assert len(passages) >= 3


def test_chunks_carry_names_notes_and_minutes(db: store.Store) -> None:
    meeting_id = _transcribed_meeting(db)
    db.rename_speaker(meeting_id, "S2", "Hazel")
    db.add_note(meeting_id, 20_000, "Ads budget of $5,000 approved")
    db.save_minutes(
        meeting_id,
        {
            "summary": "Launch agreed.",
            "decisions": ["Launch on December 1st"],
            "action_items": [{"task": "Send the beta invite", "owner": "Speaker 1", "due": "Wednesday"}],
            "topics": [{"title": "Launch date", "start_ms": 9000}],
        },
        "fake-llm",
        "GPU",
    )
    chunks = index.build_chunks(db, meeting_id)
    text = "\n".join(c.text for c in chunks)
    assert "You: Let us decide" in text
    assert "Speaker 1: I think we should launch" in text
    assert "Hazel: The onboarding videos" in text
    assert "Note typed by You: Ads budget of $5,000 approved" in text
    summary = [c for c in chunks if c.kind == index.KIND_MINUTES]
    assert len(summary) == 1
    assert "Send the beta invite (owner: Speaker 1, due: Wednesday)" in summary[0].text


# -- indexer -----------------------------------------------------------------------------


def test_indexer_stores_passages_with_embeddings(db: store.Store) -> None:
    indexer, meeting_id = _indexed(db)
    assert db.get_meeting(meeting_id).index_status == store.INDEX_DONE
    assert db.count_chunks(meeting_id) >= 2
    ids, blobs = db.chunk_embeddings(meeting_id)
    assert len(ids) == db.count_chunks(meeting_id)
    assert index.from_blobs(blobs).shape[1] == FakeEmbedder.dims
    assert db.meetings_to_index(indexer.version) == []


def test_without_the_embedding_model_keyword_search_still_works(db: store.Store) -> None:
    broken = FakeEmbedder(error=RuntimeError("offline"))
    indexer, meeting_id = _indexed(db, broken)
    assert db.chunk_embeddings(meeting_id)[0] == []
    assert indexer.status()["embeddings"] == "unavailable"
    # Indexed for keywords only, so it is indexed again once the model works.
    assert db.meetings_to_index(indexer.version) == [meeting_id]
    sources = qa.retrieve(db, indexer, "How much was the hosting quote?")
    assert sources and "$420" in sources[0].chunk.text


def test_indexer_worker_picks_up_unindexed_meetings(db: store.Store) -> None:
    meeting_id = _transcribed_meeting(db)
    untranscribed = db.create_meeting("Not yet", "Mic", None).id
    indexer = Indexer(db, FakeEmbedder())
    indexer.start()
    try:
        indexer.enqueue(untranscribed)
        assert indexer.wait_idle()
    finally:
        indexer.stop()
    assert db.count_chunks(meeting_id) > 0
    assert db.count_chunks(untranscribed) == 0


def test_reindexing_replaces_passages_and_keyword_index(db: store.Store) -> None:
    indexer, meeting_id = _indexed(db)
    assert db.keyword_search('"hazel"')
    db.rename_speaker(meeting_id, "S2", "Priya")
    indexer.index_meeting(meeting_id)
    assert db.keyword_search('"hazel"') == []
    assert db.keyword_search('"priya"')


def test_deleting_a_meeting_removes_its_passages(db: store.Store) -> None:
    _, meeting_id = _indexed(db)
    db.delete_meeting(meeting_id)
    assert db.keyword_search('"december"') == []
    assert db.chunk_embeddings()[0] == []


# -- retrieval ---------------------------------------------------------------------------


def test_fts_query_drops_filler_words() -> None:
    assert qa.fts_query("What did we decide about the launch date?") == '"decide" OR "launch" OR "date"'
    assert qa.fts_query("How much was it 420?") == '"much" OR "420"'
    assert qa.fts_query("what was said?") is None


def test_fusion_rewards_agreement() -> None:
    assert qa.fuse([[1, 2, 3], [3, 4]])[0] == 3
    assert set(qa.fuse([[1], [2]])) == {1, 2}


def test_retrieves_exact_numbers_and_scopes_to_one_meeting(db: store.Store) -> None:
    indexer, meeting_id = _indexed(db)
    other = _transcribed_meeting(
        db, "Budget review", [("mic", None, 0, 5000, "The hosting quote was $380 last year.")]
    )
    indexer.index_meeting(other)

    sources = qa.retrieve(db, indexer, "What was the hosting quote?")
    titles = {s.chunk.meeting_title for s in sources}
    assert titles == {"Launch sync", "Budget review"}
    assert [s.n for s in sources] == list(range(1, len(sources) + 1))

    scoped = qa.retrieve(db, indexer, "What was the hosting quote?", meeting_id=other)
    assert {s.chunk.meeting_id for s in scoped} == {other}


def test_unrelated_question_finds_nothing(db: store.Store) -> None:
    indexer, _ = _indexed(db)
    assert qa.retrieve(db, indexer, "What is the capital of France?") == []


# -- answers -------------------------------------------------------------------------------


def test_check_answer_keeps_real_citations_only() -> None:
    text, cited, missing = qa.check_answer("It is $420 per month [2]. Also [7] and [1, 9].", 3)
    assert text == "It is $420 per month [2]. Also and [1]."
    assert cited == [2, 1] and not missing


@pytest.mark.parametrize(
    "answer",
    ["Not found in the recordings.", "Sorry, not found in the recordings", "Paris.", "It was [5].", ""],
)
def test_uncited_or_refused_answers_are_not_found(answer: str) -> None:
    assert qa.check_answer(answer, 3) == (qa.NOT_FOUND, [], True)


def test_cited_answer_with_a_trailing_not_found_is_kept() -> None:
    # Seen from the real 3B model on a broad question.
    raw = '[2] The group discussed the challenge. [4] You said "see you then." Not found in the recordings.'
    text, cited, missing = qa.check_answer(raw, 6)
    assert not missing and cited == [2, 4]
    assert text == '[2] The group discussed the challenge. [4] You said "see you then."'


def test_prompt_numbers_sources_with_meeting_and_time(db: store.Store) -> None:
    indexer, _ = _indexed(db)
    sources = qa.retrieve(db, indexer, "Who will send the beta invite?")
    prompt = qa.build_prompt("Who will send the beta invite?", sources)
    assert prompt.startswith("Sources:\n\n[1] \"Launch sync\", ")
    assert "Question: Who will send the beta invite?" in prompt


def _asker(db: store.Store, indexer: Indexer, llm: FakeLanguageModel, **kwargs) -> qa.Asker:
    return qa.Asker(db, indexer, llm, lambda: GPU_PROFILE, **kwargs)


def test_answer_streams_and_citation_points_at_the_line(db: store.Store) -> None:
    indexer, meeting_id = _indexed(db)
    llm = FakeLanguageModel(chat_answers=["Speaker 1 will send the beta invite by Wednesday [1]."])
    events = list(_asker(db, indexer, llm).ask("Who will send the beta invite?"))

    kinds = [e["type"] for e in events]
    assert kinds[0] == "sources" and kinds[-1] == "done"
    assert "token" in kinds and {"type": "status", "state": "answering"} in events
    done = events[-1]
    assert done["answer"] == "Speaker 1 will send the beta invite by Wednesday [1]."
    assert not done["not_found"] and done["cited"] == [1]
    first = done["sources"][0]
    assert first["meeting_id"] == meeting_id
    assert first["at_ms"] == 45000  # the beta-invite line, not the passage start
    assert first["quote"] == "Speaker 1: I will send the beta invite to the 50 testers by Wednesday."
    assert llm.releases >= 1
    assert "Question: Who will send the beta invite?" in llm.chat_prompts[0]


def test_pinpoint_skips_words_repeated_from_the_question(db: store.Store) -> None:
    indexer, _ = _indexed(db)
    sources = qa.retrieve(db, indexer, "When is the launch date?")
    first = next(s for s in sources if s.chunk.start_ms == 500)
    # "launch date" is in the opening line too; "December" only where it was decided.
    at = qa.pinpoint(db, first, "The launch date is the first of December [1].", "When is the launch date?")
    assert at == 9500


def test_minutes_citation_opens_the_line_behind_it(db: store.Store) -> None:
    indexer, meeting_id = _indexed(db)
    db.save_minutes(
        meeting_id,
        {"summary": "", "decisions": [], "action_items": [
            {"task": "Send the beta invite to 50 testers", "owner": "Speaker 1", "due": "Wednesday"}
        ], "topics": []},
        "fake-llm",
        "GPU",
    )
    indexer.index_meeting(meeting_id)
    minutes_chunk = next(
        c for c in db.get_chunks(db.keyword_search('"minutes"')) if c.kind == index.KIND_MINUTES
    )
    source = qa.Source(1, minutes_chunk)
    assert qa.pinpoint(db, source, "50 testers get the invite [1].", "How many testers?") == 45000
    assert source.quote.startswith("Speaker 1: I will send the beta invite")
    assert qa.pinpoint(db, source, "Nothing in common [1].") is None
    assert source.to_dict()["at_ms"] is None


def test_no_relevant_passages_means_no_model(db: store.Store) -> None:
    indexer, _ = _indexed(db)
    llm = FakeLanguageModel(chat_answers=["Paris [1]."])
    events = list(_asker(db, indexer, llm).ask("What is the capital of France?"))
    assert events[-1]["not_found"] and events[-1]["answer"] == qa.NOT_FOUND
    assert llm.chat_prompts == [] and llm.downloads == 0


def test_model_saying_not_found_is_passed_on(db: store.Store) -> None:
    indexer, _ = _indexed(db)
    llm = FakeLanguageModel(chat_answers=["Not found in the recordings."])
    done = list(_asker(db, indexer, llm).ask("Who approved the launch budget?"))[-1]
    assert done["not_found"] and done["cited"] == []


def test_answer_falls_back_to_the_cpu(db: store.Store) -> None:
    indexer, _ = _indexed(db)
    llm = FakeLanguageModel(fail_on_gpu=True, chat_answers=["$420 per month [1]."])
    done = list(_asker(db, indexer, llm).ask("How much was the hosting quote?"))[-1]
    assert done["answer"] == "$420 per month [1]."
    assert llm.devices == ["cuda", "cpu"]


def test_errors_are_reported_and_the_lock_is_freed(db: store.Store) -> None:
    indexer, _ = _indexed(db)
    llm = FakeLanguageModel(error=ValueError("model file is damaged"))
    asker = _asker(db, indexer, llm)
    events = list(asker.ask("How much was the hosting quote?"))
    assert events[-1] == {"type": "error", "message": "model file is damaged"}
    assert not asker.busy()


def test_answering_holds_the_gpu_and_stopping_early_lets_go(db: store.Store) -> None:
    indexer, _ = _indexed(db)
    held: list[str] = []

    class Hold:
        def __enter__(self):
            held.append("in")

        def __exit__(self, *exc):
            held.append("out")

    llm = FakeLanguageModel(chat_answers=["one two three four [1]."])
    asker = _asker(db, indexer, llm, hold_gpu=Hold)
    stream = asker.ask("How much was the hosting quote?")
    for event in stream:
        if event["type"] == "token":
            assert asker.busy() and held == ["in"]
            break
    stream.close()  # the client went away mid-answer
    assert held == ["in", "out"] and not asker.busy()
    assert llm.releases >= 1


# -- sharing the GPU -----------------------------------------------------------------------


def test_queue_stays_paused_until_every_reason_resumes(db: store.Store) -> None:
    from minutemind.transcribe import TranscriptionQueue

    q = TranscriptionQueue(db, FakeEngine(), lambda: GPU_PROFILE)
    q.pause("recording")
    q.pause("ask")
    q.resume("ask")
    assert q.status()["paused"]
    q.resume("recording")
    assert not q.status()["paused"]


def test_model_manager_only_unloads_the_named_kind() -> None:
    manager = models.ModelManager()

    class Model:
        closed = False

        def close(self):
            Model.closed = True

    manager._model, manager._key = Model(), ("llama", "x", -1)
    manager.unload("whisper")
    assert manager.loaded == ("llama", "x", -1)
    manager.unload("llama")
    assert manager.loaded is None and Model.closed


# -- API ---------------------------------------------------------------------------------


@pytest.fixture
def llm() -> FakeLanguageModel:
    return FakeLanguageModel(
        chat_answers=["The launch moves to March [1]."],
    )


@pytest.fixture
def client(db: store.Store, llm: FakeLanguageModel) -> Iterator[TestClient]:
    session = RecordingSession(db, recorder_factory=FakeRecorder, device_lister=fake_devices)
    app = create_app(
        "test-token",
        db=db,
        session=session,
        engine=FakeEngine(),
        hardware_cache=FakeHardwareCache(),
        live_engine=FakeLiveEngine(),
        live_chunk_s=1.0,
        detector=FakeDetector(),
        language_model=llm,
        embedder=FakeEmbedder(),
    )
    with TestClient(app) as c:
        yield c


def _record_and_index(client: TestClient) -> int:
    body = {"mic_device_id": MIC.id, "system_device_id": "all"}
    client.post("/recording/start", json=body, headers=AUTH)
    meeting_id = client.post("/recording/stop", headers=AUTH).json()["id"]
    assert client.app.state.transcriber.wait_idle()
    assert client.app.state.indexer.wait_idle()
    return meeting_id


def _ask(client: TestClient, question: str, meeting_id: int | None = None) -> list[dict]:
    res = client.post("/ask", json={"question": question, "meeting_id": meeting_id}, headers=AUTH)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in res.text.splitlines() if line]


def test_new_transcripts_are_indexed_and_answerable(client: TestClient) -> None:
    meeting_id = _record_and_index(client)
    assert client.get(f"/meetings/{meeting_id}", headers=AUTH).json()["index_status"] == "done"

    results = client.get("/search", params={"q": "launch March"}, headers=AUTH).json()["results"]
    assert results and results[0]["meeting_id"] == meeting_id

    events = _ask(client, "When is the launch?", meeting_id)
    done = events[-1]
    assert done["type"] == "done" and done["answer"] == "The launch moves to March [1]."
    assert done["sources"][0]["meeting_id"] == meeting_id


def test_ask_unrelated_question_is_not_found(client: TestClient) -> None:
    _record_and_index(client)
    done = _ask(client, "What is the capital of France?")[-1]
    assert done["not_found"] and done["answer"] == qa.NOT_FOUND


def test_ask_is_refused_while_recording(client: TestClient) -> None:
    client.post("/recording/start", json={"mic_device_id": MIC.id}, headers=AUTH)
    res = client.post("/ask", json={"question": "When is the launch?"}, headers=AUTH)
    assert res.status_code == 409
    client.post("/recording/stop", headers=AUTH)


def test_ask_validates_input(client: TestClient) -> None:
    assert client.post("/ask", json={"question": ""}, headers=AUTH).status_code == 422
    res = client.post("/ask", json={"question": "x", "meeting_id": 999}, headers=AUTH)
    assert res.status_code == 404


def test_notes_and_renames_update_the_index(client: TestClient) -> None:
    meeting_id = _record_and_index(client)
    client.post(
        f"/meetings/{meeting_id}/notes", json={"text": "Zebra budget approved", "ts_ms": 500}, headers=AUTH
    )
    assert client.app.state.indexer.wait_idle()
    hits = client.get("/search", params={"q": "zebra"}, headers=AUTH).json()["results"]
    assert hits and "Zebra budget approved" in hits[0]["text"]

    speakers = client.get(f"/meetings/{meeting_id}/transcript", headers=AUTH).json()
    label = next(s["speaker"] for s in speakers["segments"] if s["speaker"])
    client.patch(
        f"/meetings/{meeting_id}/speakers/{label}", json={"display_name": "Quincy"}, headers=AUTH
    )
    assert client.app.state.indexer.wait_idle()
    assert client.get("/search", params={"q": "quincy"}, headers=AUTH).json()["results"]


def test_search_status(client: TestClient) -> None:
    _record_and_index(client)
    status = client.get("/search/status", headers=AUTH).json()
    assert status["embeddings"] == "ready" and status["queued"] == []


def test_ask_waits_for_a_running_job(db: store.Store, client: TestClient) -> None:
    meeting_id = _record_and_index(client)
    transcriber = client.app.state.transcriber
    release = threading.Event()
    original = transcriber._write_minutes

    def slow_minutes(mid: int) -> None:
        release.wait(5)
        original(mid)

    transcriber._write_minutes = slow_minutes
    transcriber.enqueue_minutes(meeting_id)
    while not transcriber.busy():
        pass
    result: list = []
    thread = threading.Thread(target=lambda: result.append(_ask(client, "When is the launch?")))
    thread.start()
    thread.join(0.5)
    assert thread.is_alive()  # waiting for the minutes job
    assert transcriber.status()["paused"]
    release.set()
    thread.join(5)
    assert result and result[0][-1]["type"] == "done"
    assert not transcriber.status()["paused"]
