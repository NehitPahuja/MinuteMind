"""SQLite storage for meetings.

A short-lived connection is opened per call, so the store is safe to use from
the API's worker threads and the recorder thread at the same time.
"""

from __future__ import annotations

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
]


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

    def to_dict(self) -> dict:
        return asdict(self)


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
    )
