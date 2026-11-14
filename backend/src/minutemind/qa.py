"""Ask: answer questions about past meetings, with sources.

1. Retrieval runs two ways. FTS5 keyword search (BM25) finds exact words, names
   and numbers; embedding similarity finds passages that mean the same thing.
   The two rankings are merged with reciprocal-rank fusion.
2. The best passages are numbered and given to the language model, which must
   answer only from them and cite them as [1], [2]...
3. An answer that cites nothing valid becomes "Not found in the recordings.",
   and so does any question where no passage is relevant enough to send at
   all (then the model isn't even loaded).
4. Each cited passage gets the time of the transcript line that best matches
   the answer, so a citation opens the meeting at the moment it was said.

Only one question is answered at a time, and answering holds the GPU: new
transcription jobs wait (a running one finishes first), and the live
transcript waits too.
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Callable, Iterator

import numpy as np

from minutemind import hardware, index, minutes, store
from minutemind.llm.base import LanguageModel

log = logging.getLogger(__name__)

NOT_FOUND = "Not found in the recordings."
TOP_K = 6
CANDIDATES = 20
RRF_K = 60
# Passages less similar than this to the question are never sent on meaning
# alone. bge-small scores related text about 0.6-0.8 and unrelated about 0.3.
MIN_SIMILARITY = 0.5
SOURCE_MAX_CHARS = 2_000
ANSWER_TOKENS = 400

SYSTEM_PROMPT = f"""You answer questions about the user's recorded meetings, using only the numbered sources given.
Rules:
- Use only facts stated in the sources. Never use outside knowledge and never guess.
- Copy names, numbers, amounts and dates exactly as they appear in the sources.
- After each fact, cite its source number in square brackets, like [2].
- Keep the answer short: one to three sentences.
- "You" is the person who recorded the meeting.
- If the sources don't contain the answer, reply exactly: {NOT_FOUND}"""

STOPWORDS = set(
    """a an and are as at be been but by can could did do does for from had has have he her
    his how i if in into is it its me my of on or our she so that the their them then there
    these they this to was we were what when where which who whom whose why will with would
    you your about any anything said say says tell told meeting meetings please""".split()
)

CITATION = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


@dataclass
class Source:
    n: int
    chunk: store.Chunk
    # Where a citation opens the meeting. For a minutes passage this is the
    # transcript line behind the cited fact, or None if none matched.
    at_ms: int | None = None
    # The line at at_ms, as "Name: text", once a citation was pinpointed.
    quote: str | None = None

    def to_dict(self) -> dict:
        c = self.chunk
        return {
            "n": self.n,
            "chunk_id": c.id,
            "meeting_id": c.meeting_id,
            "meeting_title": c.meeting_title,
            "meeting_created_at": c.meeting_created_at,
            "kind": c.kind,
            "start_ms": c.start_ms,
            "end_ms": c.end_ms,
            "at_ms": (
                self.at_ms
                if self.at_ms is not None or c.kind == index.KIND_MINUTES
                else c.start_ms
            ),
            "text": c.text,
            "quote": self.quote,
        }


# -- retrieval ------------------------------------------------------------------------


def keywords(text: str) -> list[str]:
    seen: list[str] = []
    for word in re.findall(r"\w+", text.lower()):
        if (word in STOPWORDS or len(word) < 2) and not word.isdigit():
            continue
        if word not in seen:
            seen.append(word)
    return seen


def fts_query(question: str) -> str | None:
    """An FTS5 query matching any of the question's meaningful words."""
    words = keywords(question)
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in words)


def fuse(rankings: list[list[int]], k: int = RRF_K) -> list[int]:
    """Reciprocal-rank fusion: ids ranked well in either list come first."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda i: -scores[i])


def retrieve(
    db: store.Store,
    indexer: index.Indexer,
    question: str,
    meeting_id: int | None = None,
    top_k: int = TOP_K,
) -> list[Source]:
    by_keyword: list[int] = []
    match = fts_query(question)
    if match:
        by_keyword = db.keyword_search(match, meeting_id, CANDIDATES)

    by_meaning: list[int] = []
    vector = indexer.embed_query(question)
    if vector is not None:
        ids, blobs = db.chunk_embeddings(meeting_id)
        if ids:
            matrix = index.from_blobs(blobs)
            if matrix.shape[1] == vector.shape[0]:
                scores = matrix @ vector
                order = np.argsort(-scores)[:CANDIDATES]
                by_meaning = [ids[i] for i in order if scores[i] >= MIN_SIMILARITY]

    chosen = fuse([by_keyword, by_meaning])[:top_k]
    return [Source(n + 1, chunk) for n, chunk in enumerate(db.get_chunks(chosen))]


# -- answering --------------------------------------------------------------------------


def build_prompt(question: str, sources: list[Source]) -> str:
    blocks = []
    for s in sources:
        c = s.chunk
        when = minutes.pretty_date(c.meeting_created_at or "")
        if c.kind == index.KIND_MINUTES:
            where = "minutes"
        else:
            where = f"{minutes.clock(c.start_ms)} to {minutes.clock(c.end_ms)}"
        text = c.text if len(c.text) <= SOURCE_MAX_CHARS else c.text[:SOURCE_MAX_CHARS] + "…"
        blocks.append(f'[{s.n}] "{c.meeting_title}", {when}, {where}\n{text}')
    return "Sources:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {question}"


def check_answer(answer: str, count: int) -> tuple[str, list[int], bool]:
    """Keep only citations of real sources. Returns (answer, cited numbers,
    not_found). An answer citing nothing is treated as not found."""
    text = answer.strip()
    if NOT_FOUND.lower().rstrip(".") in text.lower():
        return NOT_FOUND, [], True
    cited: list[int] = []

    def keep(match: re.Match) -> str:
        valid = []
        for part in match.group(1).split(","):
            n = int(part)
            if 1 <= n <= count:
                valid.append(n)
                if n not in cited:
                    cited.append(n)
        return "".join(f"[{n}]" for n in valid)

    text = CITATION.sub(keep, text)
    text = re.sub(r"\s+([.,;])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()
    if not cited or not text:
        return NOT_FOUND, [], True
    return text, cited, False


def _stem(word: str) -> str:
    """A crude stem, so "launches" matches "launch" and "testers" "tester"."""
    for suffix in ("ing", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in keywords(text)}


def pinpoint(db: store.Store, source: Source, answer: str, question: str = "") -> int | None:
    """Find the transcript line (or note) that best matches the answer, store
    it on the source as `at_ms` and `quote`, and return its start.

    Words the answer only repeats from the question ("the mobile app launches
    on December 1st") say nothing about where the fact is, so the words the
    answer adds count first. A transcript passage is searched within itself;
    a minutes passage, which has no times, across the whole meeting.
    """
    c = source.chunk
    if c.meeting_id is None:
        return None
    said = _stems(CITATION.sub(" ", answer))
    wanted = (said - _stems(question)) or said
    minutes_passage = c.kind == index.KIND_MINUTES
    names = {s["label"]: s["display_name"] for s in db.list_speakers(c.meeting_id)}
    candidates = [
        (s.start_ms, s.text, f"{minutes.speaker_name(s, names)}: {s.text.strip()}")
        for s in db.get_segments(c.meeting_id)
    ]
    candidates += [
        (n.ts_ms, n.text, f"Note typed by {minutes.YOU}: {n.text.strip()}")
        for n in db.list_notes(c.meeting_id)
    ]
    best: tuple[int, int, str] | None = None  # (overlap, -start, quote)
    for start, text, quote in candidates:
        if not minutes_passage and not c.start_ms <= start <= c.end_ms:
            continue
        overlap = len(wanted & _stems(text))
        if overlap and (best is None or (overlap, -start) > best[:2]):
            best = (overlap, -start, quote)
    if best:
        source.at_ms, source.quote = -best[1], best[2]
    else:
        source.at_ms = None if minutes_passage else c.start_ms
    return source.at_ms


class Asker:
    def __init__(
        self,
        db: store.Store,
        indexer: index.Indexer,
        llm: LanguageModel,
        profile: Callable[[], hardware.Profile],
        hold_gpu: Callable[[], AbstractContextManager] = nullcontext,
    ) -> None:
        self.db = db
        self.indexer = indexer
        self.llm = llm
        self._profile = profile
        self._hold_gpu = hold_gpu
        self._lock = threading.Lock()

    def busy(self) -> bool:
        return self._lock.locked()

    def ask(self, question: str, meeting_id: int | None = None) -> Iterator[dict]:
        """Answer a question, yielding events as the answer is written:
        sources, status (waiting / loading / answering), token, then done
        (or error). Closing the iterator early stops the answer."""
        sources = retrieve(self.db, self.indexer, question, meeting_id)
        yield {"type": "sources", "sources": [s.to_dict() for s in sources]}
        if not sources:
            yield self._done(NOT_FOUND, [], True, [])
            return

        if not self._lock.acquire(blocking=False):
            yield {"type": "status", "state": "queued"}
            self._lock.acquire()
        try:
            yield {"type": "status", "state": "waiting"}
            with self._hold_gpu():
                yield {"type": "status", "state": "loading"}
                try:
                    self.llm.ensure_model(lambda _p: None)
                    pieces = []
                    for piece in self._stream(question, sources):
                        if not pieces:
                            yield {"type": "status", "state": "answering"}
                        pieces.append(piece)
                        yield {"type": "token", "text": piece}
                finally:
                    self.llm.release()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            log.exception("Answering failed")
            yield {"type": "error", "message": str(exc) or exc.__class__.__name__}
            return
        finally:
            self._lock.release()

        answer, cited, not_found = check_answer("".join(pieces), len(sources))
        for s in sources:
            if s.n in cited:
                pinpoint(self.db, s, answer, question)
        yield self._done(answer, cited, not_found, sources)

    def _stream(self, question: str, sources: list[Source]) -> Iterator[str]:
        from minutemind.transcribe import is_gpu_failure

        prompt = build_prompt(question, sources)
        profile = self._profile()
        started = False
        try:
            for piece in self.llm.stream_chat(SYSTEM_PROMPT, prompt, profile, ANSWER_TOKENS):
                started = True
                yield piece
        except Exception as exc:
            if started or profile.device == "cpu" or not is_gpu_failure(exc):
                raise
            log.warning("Answering on the GPU failed (%s); retrying on the CPU", exc)
            self.llm.release()
            yield from self.llm.stream_chat(
                SYSTEM_PROMPT, prompt, hardware.CPU_PROFILE, ANSWER_TOKENS
            )

    @staticmethod
    def _done(answer: str, cited: list[int], not_found: bool, sources: list[Source]) -> dict:
        return {
            "type": "done",
            "answer": answer,
            "not_found": not_found,
            "cited": cited,
            "sources": [s.to_dict() for s in sources],
        }
