from __future__ import annotations

from minutemind import paths, store


def test_meeting_lifecycle(db: store.Store) -> None:
    m = db.create_meeting("Standup", mic_device="Mic", system_device=None)
    assert m.status == store.RECORDING
    assert m.has_mic and not m.has_system

    db.set_status(m.id, store.PAUSED)
    assert [a.id for a in db.active_meetings()] == [m.id]

    done = db.finish_meeting(m.id, 61_000)
    assert done.status == store.COMPLETED
    assert done.duration_ms == 61_000
    assert done.ended_at is not None
    assert db.active_meetings() == []

    renamed = db.rename_meeting(m.id, "Daily standup")
    assert renamed is not None and renamed.title == "Daily standup"


def test_list_newest_first(db: store.Store) -> None:
    first = db.create_meeting("A", "Mic", None)
    second = db.create_meeting("B", "Mic", None)
    assert [m.id for m in db.list_meetings()] == [second.id, first.id]


def test_delete_removes_audio_folder(db: store.Store) -> None:
    m = db.create_meeting("Gone", "Mic", None)
    folder = paths.meeting_dir(m.id)
    (folder / "mic.wav").write_bytes(b"x")
    assert db.delete_meeting(m.id) is True
    assert db.get_meeting(m.id) is None
    assert not folder.exists()
    assert db.delete_meeting(m.id) is False


def test_reopening_does_not_rerun_migrations(db: store.Store) -> None:
    db.create_meeting("Keep", "Mic", None)
    again = store.Store()
    assert [m.title for m in again.list_meetings()] == ["Keep"]
