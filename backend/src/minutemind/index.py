"""The search index: meetings cut into passages, with keyword and meaning search.

Each meeting's transcript (speaker names and typed notes included) is cut into
passages of about 45 seconds that overlap a little, so an answer spanning a
cut still lands whole in one of them. The minutes become one more passage.
Passages go into the `chunks` table, where SQLite's FTS5 indexes their words
(see store.py), and get an embedding from bge-small for meaning search.

Indexing runs on its own worker thread on the processor. It never touches the
GPU, so it doesn't wait behind transcription jobs. If the embedding model
can't be downloaded, meetings are indexed for keyword search only and indexed
again once the model is there.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from minutemind import minutes, models, store

log = logging.getLogger(__name__)

WINDOW_MS = 45_000
OVERLAP_MS = 10_000
MAX_CHARS = 1_200
# Lines of the previous passage carried into the next one, at most.
OVERLAP_LINES = 2
EMBED_BATCH = 16
# After the embedding model fails to download, wait this long before trying again.
RETRY_DOWNLOAD_S = 600

KIND_TRANSCRIPT = "transcript"
KIND_MINUTES = "minutes"


# -- passages ---------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    start_ms: int
    end_ms: int
    text: str


def meeting_items(db: store.Store, meeting_id: int) -> list[Item]:
    """Transcript lines as "Name: text", plus typed notes, in time order."""
    names = {s["label"]: s["display_name"] for s in db.list_speakers(meeting_id)}
    items = [
        Item(s.start_ms, s.end_ms, f"{minutes.speaker_name(s, names)}: {s.text.strip()}")
        for s in db.get_segments(meeting_id)
        if s.text.strip()
    ]
    items += [
        Item(n.ts_ms, n.ts_ms, f"Note typed by {minutes.YOU}: {n.text.strip()}")
        for n in db.list_notes(meeting_id)
        if n.text.strip()
    ]
    return sorted(items, key=lambda i: (i.start_ms, i.end_ms))


def cut_passages(items: list[Item]) -> list[store.Chunk]:
    """Group items into passages of about WINDOW_MS, overlapping by a line or two."""
    passages: list[store.Chunk] = []
    current: list[Item] = []

    def emit() -> None:
        passages.append(
            store.Chunk(
                kind=KIND_TRANSCRIPT,
                start_ms=current[0].start_ms,
                end_ms=max(i.end_ms for i in current),
                text="\n".join(i.text for i in current),
            )
        )

    for item in items:
        if current:
            too_long = item.end_ms - current[0].start_ms > WINDOW_MS
            too_big = sum(len(i.text) + 1 for i in current) + len(item.text) > MAX_CHARS
            if too_long or too_big:
                emit()
                end = max(i.end_ms for i in current)
                tail = [i for i in current if i.start_ms >= end - OVERLAP_MS][-OVERLAP_LINES:]
                if len(tail) == len(current):
                    tail = tail[1:]  # always move forward
                current = tail
        current.append(item)
    if current:
        emit()
    return passages


def minutes_passage(db: store.Store, meeting_id: int) -> store.Chunk | None:
    stored = db.get_minutes(meeting_id)
    meeting = db.get_meeting(meeting_id)
    if not stored or meeting is None:
        return None
    content = stored["content"]
    parts = ["Meeting minutes."]
    if content.get("summary"):
        parts.append(f"Summary: {content['summary']}")
    if content.get("decisions"):
        parts.append("Decisions: " + "; ".join(content["decisions"]))
    if content.get("action_items"):
        tasks = []
        for item in content["action_items"]:
            extra = ", ".join(
                f"{label}: {item[key]}"
                for key, label in (("owner", "owner"), ("due", "due"))
                if item.get(key)
            )
            tasks.append(f"{item['task']} ({extra})" if extra else item["task"])
        parts.append("Action items: " + "; ".join(tasks))
    if content.get("topics"):
        parts.append("Topics: " + "; ".join(t["title"] for t in content["topics"]))
    return store.Chunk(
        kind=KIND_MINUTES, start_ms=0, end_ms=meeting.duration_ms, text="\n".join(parts)
    )


def build_chunks(db: store.Store, meeting_id: int) -> list[store.Chunk]:
    chunks = cut_passages(meeting_items(db, meeting_id))
    summary = minutes_passage(db, meeting_id)
    if summary is not None:
        chunks.append(summary)
    return chunks


# -- embeddings ---------------------------------------------------------------------


class Embedder(Protocol):
    name: str

    def ensure(self) -> None:
        """Download and load the model; raises if it can't."""
        ...

    def embed(self, texts: list[str], query: bool = False) -> np.ndarray:
        """Unit-length float32 vectors, one row per text."""
        ...


class OnnxEmbedder:
    """bge-small-en-v1.5 run with onnxruntime on the processor."""

    def __init__(self, spec: models.EmbedderSpec = models.EMBEDDER) -> None:
        self.spec = spec
        self.name = spec.name
        self._session = None
        self._tokenizer = None
        self._lock = threading.Lock()

    def ensure(self) -> None:
        with self._lock:
            if self._session is not None:
                return
            folder = models.download_embedder(self.spec)
            import onnxruntime as ort
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
            tokenizer.enable_truncation(max_length=512)
            tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            options = ort.SessionOptions()
            options.intra_op_num_threads = 4
            self._session = ort.InferenceSession(
                str(folder / "model.onnx"), options, providers=["CPUExecutionProvider"]
            )
            self._tokenizer = tokenizer
            log.info("Loaded embedding model %s", self.name)

    def embed(self, texts: list[str], query: bool = False) -> np.ndarray:
        self.ensure()
        if query:
            texts = [self.spec.query_prefix + t for t in texts]
        out = []
        with self._lock:
            for i in range(0, len(texts), EMBED_BATCH):
                encodings = self._tokenizer.encode_batch(texts[i : i + EMBED_BATCH])
                ids = np.array([e.ids for e in encodings], dtype=np.int64)
                mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
                hidden = self._session.run(
                    None,
                    {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)},
                )[0]
                out.append(hidden[:, 0])  # the [CLS] token, as bge is trained
        return normalize(np.concatenate(out) if out else np.zeros((0, self.spec.dims)))


def normalize(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blobs(blobs: list[bytes]) -> np.ndarray:
    if not blobs:
        return np.zeros((0, 0), dtype=np.float32)
    return np.stack([np.frombuffer(b, dtype=np.float32) for b in blobs])


# -- worker -------------------------------------------------------------------------


class Indexer:
    """Keeps every transcribed meeting's passages indexed, one at a time."""

    def __init__(
        self,
        db: store.Store,
        embedder: Embedder | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.embedder = embedder
        self._clock = clock
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._queued: list[int] = []
        self._current: int | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # "unknown" until first used, then "ready" or "unavailable".
        self._embedder_state = "unknown"
        self._embedder_error: str | None = None
        self._failed_at: float | None = None

    @property
    def version(self) -> str:
        return self.embedder.name if self.embedder is not None else store.INDEX_KEYWORDS

    def start(self) -> None:
        self._thread = threading.Thread(target=self._work, name="indexer", daemon=True)
        self._thread.start()
        for meeting_id in self.db.meetings_to_index(self.version):
            self.enqueue(meeting_id)

    def stop(self, timeout: float = 5) -> None:
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout)

    def enqueue(self, meeting_id: int) -> None:
        with self._lock:
            if meeting_id in self._queued:
                return
            self._queued.append(meeting_id)
        self._queue.put(meeting_id)

    def status(self) -> dict:
        with self._lock:
            return {
                "current": self._current,
                "queued": list(self._queued),
                "embeddings": self._embedder_state,
                "embeddings_error": self._embedder_error,
            }

    def wait_idle(self, timeout: float = 30) -> bool:
        """For tests and scripts: block until nothing is queued or running."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if status["current"] is None and not status["queued"]:
                return True
            time.sleep(0.02)
        return False

    def embed_query(self, text: str) -> np.ndarray | None:
        """The question's vector, or None when meaning search isn't available."""
        if not self._embedder_ready():
            return None
        try:
            return self.embedder.embed([text], query=True)[0]  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - keyword search still works
            log.exception("Embedding the question failed")
            return None

    def _embedder_ready(self) -> bool:
        if self.embedder is None:
            return False
        with self._lock:
            if self._embedder_state == "ready":
                return True
            if self._failed_at is not None and self._clock() - self._failed_at < RETRY_DOWNLOAD_S:
                return False
        try:
            self.embedder.ensure()
        except Exception as exc:  # noqa: BLE001 - fall back to keywords
            log.warning("Embedding model unavailable, using keyword search only: %s", exc)
            with self._lock:
                self._embedder_state = "unavailable"
                self._embedder_error = str(exc)
                self._failed_at = self._clock()
            return False
        with self._lock:
            self._embedder_state = "ready"
            self._embedder_error = None
        return True

    def _work(self) -> None:
        while True:
            meeting_id = self._queue.get()
            if meeting_id is None:
                return
            with self._lock:
                if meeting_id in self._queued:
                    self._queued.remove(meeting_id)
                self._current = meeting_id
            try:
                self.index_meeting(meeting_id)
            except Exception:  # noqa: BLE001 - recorded on the meeting
                log.exception("Indexing meeting %s failed", meeting_id)
                if self.db.get_meeting(meeting_id) is not None:
                    self.db.set_index_status(meeting_id, store.INDEX_FAILED)
            finally:
                with self._lock:
                    self._current = None

    def index_meeting(self, meeting_id: int) -> None:
        meeting = self.db.get_meeting(meeting_id)
        if meeting is None or meeting.transcript_status != store.TRANSCRIPT_DONE:
            return  # deleted, or nothing to index yet
        chunks = build_chunks(self.db, meeting_id)
        version = store.INDEX_KEYWORDS
        if chunks and self._embedder_ready():
            vectors = self.embedder.embed([c.text for c in chunks])  # type: ignore[union-attr]
            chunks = [
                store.Chunk(c.kind, c.start_ms, c.end_ms, c.text, to_blob(v))
                for c, v in zip(chunks, vectors)
            ]
            version = self.version
        elif not chunks:
            version = self.version  # nothing to embed, so nothing is missing
        self.db.replace_chunks(meeting_id, chunks, version)
        log.info("Indexed meeting %s: %d passages (%s)", meeting_id, len(chunks), version)
