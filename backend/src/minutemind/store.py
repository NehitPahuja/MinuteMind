"""SQLite storage for meetings.

A short-lived connection is opened per call, so the store is safe to use from
the API's worker threads and the recorder thread at the same time.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from minutemind import paths

# Meeting status values.
RECORDING = "recording"
PAUSED = "paused"
COMPLETED = "completed"
RECOVERED = "recovered"  # app closed or crashed mid-recording; audio was salvaged
FAILED = "failed"

ACTIVE_STATUSES = (RECORDING, PAUSED)

# Transcript status values.
TRANSCRIPT_NONE = "none"
TRANSCRIPT_QUEUED = "queued"
TRANSCRIPT_RUNNING = "running"
TRANSCRIPT_DONE = "done"
TRANSCRIPT_FAILED = "failed"
TRANSCRIPT_PENDING = (TRANSCRIPT_QUEUED, TRANSCRIPT_RUNNING)

# Settings and their defaults. Values are stored as JSON.
DEFAULT_SETTINGS: dict[str, object] = {
    # "auto" picks GPU or CPU from the hardware; "gpu" / "cpu" force one.
    "processing": "auto",
    # Start transcribing as soon as a recording stops.
    "auto_transcribe": True,
    # Show a rough transcript while recording.
    "live_transcript": True,
    # Tell apart the people on the other side of the call.
    "speaker_labels": True,
    # Write minutes with the local AI once the transcript is ready.
    "auto_minutes": True,
}

# Speaker detection status values (per meeting).
SPEAKERS_NONE = "none"
SPEAKERS_DONE = "done"
SPEAKERS_FAILED = "failed"

# Minutes status values (per meeting).
MINUTES_NONE = "none"
MINUTES_QUEUED = "queued"
MINUTES_RUNNING = "running"
MINUTES_DONE = "done"
MINUTES_FAILED = "failed"
MINUTES_PENDING = (MINUTES_QUEUED, MINUTES_RUNNING)

# Each entry upgrades the schema by one version. Never edit an entry once shipped.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE meetings (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        title         TEXT    NOT NULL,
        created_at    TEXT    NOT NULL,
        ended_at      TEXT,
        duration_ms   INTEGER NOT NULL DEFAULT 0,
        status        TEXT    NOT NULL,
        mic_device    TEXT,
        system_device TEXT,
        has_mic       INTEGER NOT NULL DEFAULT 0,
        has_system    INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 2: transcripts and settings
    """
    ALTER TABLE meetings ADD COLUMN transcript_status TEXT NOT NULL DEFAULT 'none';
    ALTER TABLE meetings ADD COLUMN transcript_error TEXT;
    ALTER TABLE meetings ADD COLUMN transcript_model TEXT;
    ALTER TABLE meetings ADD COLUMN transcript_device TEXT;

    CREATE TABLE segments (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
        source     TEXT    NOT NULL,  -- 'mic' (you) or 'system' (others)
        start_ms   INTEGER NOT NULL,
        end_ms     INTEGER NOT NULL,
        text       TEXT    NOT NULL,
        words      TEXT    NOT NULL DEFAULT '[]'  -- JSON [[start_ms, end_ms, word], ...]
    );
    CREATE INDEX segments_by_meeting ON segments (meeting_id, start_ms);

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 3: notes typed during or after a meeting
    """
    CREATE TABLE notes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
        ts_ms      INTEGER NOT NULL,  -- moment in the meeting the note refers to
        text       TEXT    NOT NULL,
        created_at TEXT    NOT NULL,
        updated_at TEXT    NOT NULL
    );
    CREATE INDEX notes_by_meeting ON notes (meeting_id, ts_ms);
    """,
    # 4: speakers on the other side of the call
    """
    ALTER TABLE segments ADD COLUMN speaker TEXT;  -- e.g. 'S1'; NULL for the mic ("you")
    ALTER TABLE meetings ADD COLUMN speakers_status TEXT NOT NULL DEFAULT 'none';
    ALTER TABLE meetings ADD COLUMN speakers_error TEXT;

    CREATE TABLE speakers (
        meeting_id   INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
        label        TEXT    NOT NULL,
        display_name TEXT    NOT NULL,
        PRIMARY KEY (meeting_id, label)
    );
    """,
    # 5: meeting minutes written by the language model
    """
    ALTER TABLE meetings ADD COLUMN minutes_status TEXT NOT NULL DEFAULT 'none';
    ALTER TABLE meetings ADD COLUMN minutes_error TEXT;

    CREATE TABLE minutes (
        meeting_id INTEGER PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
        content    TEXT NOT NULL,  -- JSON: summary, decisions, action_items, topics
        model      TEXT NOT NULL,
        device     TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    # 6: search index. Each meeting's transcript (with notes) is cut into
    # overlapping passages, plus one passage for its minutes. chunks_fts is a
    # keyword index over the passages, kept in step by the triggers.
    """
    ALTER TABLE meetings ADD COLUMN index_status TEXT NOT NULL DEFAULT 'none';
    ALTER TABLE meetings ADD COLUMN index_version TEXT;  -- embedding model, or 'keywords'

    CREATE TABLE chunks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
        kind       TEXT    NOT NULL,  -- 'transcript' or 'minutes'
        start_ms   INTEGER NOT NULL,
        end_ms     INTEGER NOT NULL,
        text       TEXT    NOT NULL,
        embedding  BLOB              -- float32 vector, NULL without the embedding model
    );
    CREATE INDEX chunks_by_meeting ON chunks (meeting_id, start_ms);

    CREATE VIRTUAL TABLE chunks_fts USING fts5(
        text, content='chunks', content_rowid='id', tokenize='porter unicode61'
    );
    CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts (rowid, text) VALUES (new.id, new.text);
    END;
    CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    END;
    CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
        INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
        INSERT INTO chunks_fts (rowid, text) VALUES (new.id, new.text);
    END;
    """,
]

# Search index status values (per meeting).
INDEX_NONE = "none"
INDEX_DONE = "done"
INDEX_FAILED = "failed"
INDEX_KEYWORDS = "keywords"  # index_version when no embedding model was available


@dataclass(frozen=True)
class Meeting:
    id: int
    title: str
    created_at: str
    ended_at: str | None
    duration_ms: int
    status: str
    mic_device: str | None
    system_device: str | None
    has_mic: bool
    has_system: bool
    transcript_status: str = TRANSCRIPT_NONE
    transcript_error: str | None = None
    transcript_model: str | None = None
    transcript_device: str | None = None
    speakers_status: str = SPEAKERS_NONE
    speakers_error: str | None = None
    minutes_status: str = MINUTES_NONE
    minutes_error: str | None = None
    index_status: str = INDEX_NONE

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Segment:
    source: str  # "mic" | "system"
    start_ms: int
    end_ms: int
    text: str
    words: list[tuple[int, int, str]]
    speaker: str | None = None  # "S1", "S2", ... for PC sound once detected
    id: int | None = None

    def to_dict(self, with_words: bool = False) -> dict:
        data = {
            "id": self.id,
            "source": self.source,
            "speaker": self.speaker,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
        }
        if with_words:
            data["words"] = [list(w) for w in self.words]
        return data


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or paths.db_path()
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for index in range(version, len(MIGRATIONS)):
                conn.executescript(MIGRATIONS[index])
                conn.execute(f"PRAGMA user_version = {index + 1}")

    # -- meetings ---------------------------------------------------------

    def create_meeting(
        self, title: str, mic_device: str | None, system_device: str | None
    ) -> Meeting:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO meetings (title, created_at, status, mic_device, system_device,"
                " has_mic, has_system) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    title,
                    _now(),
                    RECORDING,
                    mic_device,
                    system_device,
                    int(mic_device is not None),
                    int(system_device is not None),
                ),
            )
            meeting_id = cur.lastrowid
        assert meeting_id is not None
        return self.get_meeting(meeting_id)  # type: ignore[return-value]

    def set_devices(
        self, meeting_id: int, mic_device: str | None, system_device: str | None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE meetings SET mic_device = ?, system_device = ?,"
                " has_mic = ?, has_system = ? WHERE id = ?",
                (
                    mic_device,
                    system_device,
                    int(mic_device is not None),
                    int(system_device is not None),
                    meeting_id,
                ),
            )

    def set_status(self, meeting_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE meetings SET status = ? WHERE id = ?", (status, meeting_id))

    def finish_meeting(
        self,
        meeting_id: int,
        duration_ms: int,
        status: str = COMPLETED,
        has_mic: bool | None = None,
        has_system: bool | None = None,
    ) -> Meeting:
        with self._connect() as conn:
            conn.execute(
                "UPDATE meetings SET ended_at = ?, duration_ms = ?, status = ?,"
                " has_mic = COALESCE(?, has_mic), has_system = COALESCE(?, has_system)"
                " WHERE id = ?",
                (
                    _now(),
                    duration_ms,
                    status,
                    None if has_mic is None else int(has_mic),
                    None if has_system is None else int(has_system),
                    meeting_id,
                ),
            )
        return self.get_meeting(meeting_id)  # type: ignore[return-value]

    def rename_meeting(self, meeting_id: int, title: str) -> Meeting | None:
        with self._connect() as conn:
            conn.execute("UPDATE meetings SET title = ? WHERE id = ?", (title, meeting_id))
        return self.get_meeting(meeting_id)

    def delete_meeting(self, meeting_id: int) -> bool:
        with self._connect() as conn:
            deleted = conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,)).rowcount
        if deleted:
            shutil.rmtree(paths.data_dir() / "meetings" / str(meeting_id), ignore_errors=True)
        return bool(deleted)

    def get_meeting(self, meeting_id: int) -> Meeting | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return _row_to_meeting(row) if row else None

    def list_meetings(self) -> list[Meeting]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM meetings ORDER BY created_at DESC, id DESC").fetchall()
        return [_row_to_meeting(r) for r in rows]

    def active_meetings(self) -> list[Meeting]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM meetings WHERE status IN (?, ?)", ACTIVE_STATUSES
            ).fetchall()
        return [_row_to_meeting(r) for r in rows]

    # -- transcripts ------------------------------------------------------

    def set_transcript_status(
        self,
        meeting_id: int,
        status: str,
        error: str | None = None,
        model: str | None = None,
        device: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE meetings SET transcript_status = ?, transcript_error = ?,"
                " transcript_model = COALESCE(?, transcript_model),"
                " transcript_device = COALESCE(?, transcript_device) WHERE id = ?",
                (status, error, model, device, meeting_id),
            )

    def pending_transcripts(self) -> list[int]:
        """Meetings queued or mid-transcription (e.g. when the app last closed)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM meetings WHERE transcript_status IN (?, ?) ORDER BY id",
                TRANSCRIPT_PENDING,
            ).fetchall()
        return [r["id"] for r in rows]

    def replace_segments(self, meeting_id: int, segments: list[Segment]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM segments WHERE meeting_id = ?", (meeting_id,))
            conn.executemany(
                "INSERT INTO segments (meeting_id, source, start_ms, end_ms, text, words, speaker)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        meeting_id,
                        seg.source,
                        seg.start_ms,
                        seg.end_ms,
                        seg.text,
                        json.dumps([list(w) for w in seg.words]),
                        seg.speaker,
                    )
                    for seg in segments
                ],
            )

    def get_segments(self, meeting_id: int) -> list[Segment]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM segments WHERE meeting_id = ? ORDER BY start_ms, id",
                (meeting_id,),
            ).fetchall()
        return [
            Segment(
                id=r["id"],
                source=r["source"],
                start_ms=r["start_ms"],
                end_ms=r["end_ms"],
                text=r["text"],
                words=[tuple(w) for w in json.loads(r["words"])],
                speaker=r["speaker"],
            )
            for r in rows
        ]

    # -- speakers ---------------------------------------------------------

    def set_speakers(self, meeting_id: int, labels: list[str]) -> None:
        """Replace a meeting's speakers with fresh default names."""
        with self._connect() as conn:
            conn.execute("DELETE FROM speakers WHERE meeting_id = ?", (meeting_id,))
            conn.executemany(
                "INSERT INTO speakers (meeting_id, label, display_name) VALUES (?, ?, ?)",
                [(meeting_id, label, f"Speaker {label[1:]}") for label in labels],
            )

    def list_speakers(self, meeting_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.label, s.display_name,"
                " (SELECT COUNT(*) FROM segments g WHERE g.meeting_id = s.meeting_id"
                "  AND g.speaker = s.label) AS lines"
                " FROM speakers s WHERE s.meeting_id = ?"
                " ORDER BY CAST(SUBSTR(s.label, 2) AS INTEGER)",
                (meeting_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def rename_speaker(self, meeting_id: int, label: str, display_name: str) -> bool:
        with self._connect() as conn:
            return bool(
                conn.execute(
                    "UPDATE speakers SET display_name = ? WHERE meeting_id = ? AND label = ?",
                    (display_name, meeting_id, label),
                ).rowcount
            )

    def set_speakers_status(self, meeting_id: int, status: str, error: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE meetings SET speakers_status = ?, speakers_error = ? WHERE id = ?",
                (status, error, meeting_id),
            )

    # -- minutes ----------------------------------------------------------

    def set_minutes_status(self, meeting_id: int, status: str, error: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE meetings SET minutes_status = ?, minutes_error = ? WHERE id = ?",
                (status, error, meeting_id),
            )

    def save_minutes(self, meeting_id: int, content: dict, model: str, device: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO minutes (meeting_id, content, model, device, created_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(meeting_id) DO UPDATE SET"
                " content = excluded.content, model = excluded.model,"
                " device = excluded.device, created_at = excluded.created_at",
                (meeting_id, json.dumps(content), model, device, _now()),
            )
            conn.execute(
                "UPDATE meetings SET minutes_status = ?, minutes_error = NULL WHERE id = ?",
                (MINUTES_DONE, meeting_id),
            )

    def get_minutes(self, meeting_id: int) -> dict | None:
        """The saved minutes with when and how they were written, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM minutes WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "content": json.loads(row["content"]),
            "model": row["model"],
            "device": row["device"],
            "created_at": row["created_at"],
        }

    def pending_minutes(self) -> list[int]:
        """Meetings whose minutes were queued or being written (e.g. when the app last closed)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM meetings WHERE minutes_status IN (?, ?)"
                " AND transcript_status NOT IN (?, ?) ORDER BY id",
                (*MINUTES_PENDING, *TRANSCRIPT_PENDING),
            ).fetchall()
        return [r["id"] for r in rows]

    # -- search index -----------------------------------------------------

    def replace_chunks(self, meeting_id: int, chunks: list[Chunk], version: str) -> None:
        """Swap in a meeting's passages (with their keyword index) in one go."""
        with self._connect() as conn:
            conn.execute("DELETE FROM chunks WHERE meeting_id = ?", (meeting_id,))
            conn.executemany(
                "INSERT INTO chunks (meeting_id, kind, start_ms, end_ms, text, embedding)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (meeting_id, c.kind, c.start_ms, c.end_ms, c.text, c.embedding)
                    for c in chunks
                ],
            )
            conn.execute(
                "UPDATE meetings SET index_status = ?, index_version = ? WHERE id = ?",
                (INDEX_DONE, version, meeting_id),
            )

    def set_index_status(self, meeting_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE meetings SET index_status = ? WHERE id = ?", (status, meeting_id))

    def meetings_to_index(self, version: str) -> list[int]:
        """Meetings with a transcript whose index is missing or was built
        with a different embedding model (or none)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM meetings WHERE transcript_status = ?"
                " AND (index_version IS NULL OR index_version != ?) ORDER BY id",
                (TRANSCRIPT_DONE, version),
            ).fetchall()
        return [r["id"] for r in rows]

    def keyword_search(
        self, match: str, meeting_id: int | None = None, limit: int = 20
    ) -> list[int]:
        """Chunk ids matching an FTS5 query, best first (BM25)."""
        sql = (
            "SELECT c.id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            " WHERE chunks_fts MATCH ?"
        )
        args: list[object] = [match]
        if meeting_id is not None:
            sql += " AND c.meeting_id = ?"
            args.append(meeting_id)
        sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [r["id"] for r in conn.execute(sql, args).fetchall()]

    def chunk_embeddings(self, meeting_id: int | None = None) -> tuple[list[int], list[bytes]]:
        """Ids and raw vectors of every chunk that has one (in scope)."""
        sql = "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL"
        args: tuple = ()
        if meeting_id is not None:
            sql += " AND meeting_id = ?"
            args = (meeting_id,)
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [r["id"] for r in rows], [r["embedding"] for r in rows]

    def get_chunks(self, ids: list[int]) -> list[Chunk]:
        """Chunks by id, in the order asked for, with their meeting's title."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.*, m.title, m.created_at FROM chunks c JOIN meetings m ON m.id = c.meeting_id"
                f" WHERE c.id IN ({marks})",
                ids,
            ).fetchall()
        by_id = {
            r["id"]: Chunk(
                kind=r["kind"],
                start_ms=r["start_ms"],
                end_ms=r["end_ms"],
                text=r["text"],
                id=r["id"],
                meeting_id=r["meeting_id"],
                meeting_title=r["title"],
                meeting_created_at=r["created_at"],
            )
            for r in rows
        }
        return [by_id[i] for i in ids if i in by_id]

    def count_chunks(self, meeting_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE meeting_id = ?", (meeting_id,)
            ).fetchone()
        return int(row["n"])

    # -- notes ------------------------------------------------------------

    def add_note(self, meeting_id: int, ts_ms: int, text: str) -> Note:
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO notes (meeting_id, ts_ms, text, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (meeting_id, ts_ms, text, now, now),
            )
            note_id = cur.lastrowid
        assert note_id is not None
        return self.get_note(note_id)  # type: ignore[return-value]

    def get_note(self, note_id: int) -> Note | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return _row_to_note(row) if row else None

    def list_notes(self, meeting_id: int) -> list[Note]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notes WHERE meeting_id = ? ORDER BY ts_ms, id", (meeting_id,)
            ).fetchall()
        return [_row_to_note(r) for r in rows]

    def update_note(
        self, note_id: int, text: str | None = None, ts_ms: int | None = None
    ) -> Note | None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE notes SET text = COALESCE(?, text), ts_ms = COALESCE(?, ts_ms),"
                " updated_at = ? WHERE id = ?",
                (text, ts_ms, _now(), note_id),
            )
        return self.get_note(note_id)

    def delete_note(self, note_id: int) -> bool:
        with self._connect() as conn:
            return bool(conn.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount)

    # -- settings ---------------------------------------------------------

    def get_settings(self) -> dict[str, object]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        stored = {r["key"]: json.loads(r["value"]) for r in rows}
        return {key: stored.get(key, default) for key, default in DEFAULT_SETTINGS.items()}

    def update_settings(self, changes: dict[str, object]) -> dict[str, object]:
        unknown = set(changes) - set(DEFAULT_SETTINGS)
        if unknown:
            raise KeyError(f"Unknown settings: {', '.join(sorted(unknown))}")
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(key, json.dumps(value)) for key, value in changes.items()],
            )
        return self.get_settings()


@dataclass(frozen=True)
class Chunk:
    """A passage of a meeting, as stored for search."""

    kind: str  # "transcript" or "minutes"
    start_ms: int
    end_ms: int
    text: str
    embedding: bytes | None = None
    id: int | None = None
    meeting_id: int | None = None
    meeting_title: str | None = None
    meeting_created_at: str | None = None


@dataclass(frozen=True)
class Note:
    id: int
    meeting_id: int
    ts_ms: int
    text: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        meeting_id=row["meeting_id"],
        ts_ms=row["ts_ms"],
        text=row["text"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_meeting(row: sqlite3.Row) -> Meeting:
    return Meeting(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"],
        ended_at=row["ended_at"],
        duration_ms=row["duration_ms"],
        status=row["status"],
        mic_device=row["mic_device"],
        system_device=row["system_device"],
        has_mic=bool(row["has_mic"]),
        has_system=bool(row["has_system"]),
        transcript_status=row["transcript_status"],
        transcript_error=row["transcript_error"],
        transcript_model=row["transcript_model"],
        transcript_device=row["transcript_device"],
        speakers_status=row["speakers_status"],
        speakers_error=row["speakers_error"],
        minutes_status=row["minutes_status"],
        minutes_error=row["minutes_error"],
        index_status=row["index_status"],
    )
