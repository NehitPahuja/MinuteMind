"""Where MinuteMind keeps its data on disk.

Default: %APPDATA%\\MinuteMind on Windows (~/.minutemind elsewhere).
Override with the MINUTEMIND_DATA_DIR environment variable (used by tests).
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("MINUTEMIND_DATA_DIR")
    if override:
        root = Path(override)
    elif os.name == "nt" and os.environ.get("APPDATA"):
        root = Path(os.environ["APPDATA"]) / "MinuteMind"
    else:
        root = Path.home() / ".minutemind"
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return data_dir() / "minutemind.db"


def meeting_dir(meeting_id: int) -> Path:
    path = data_dir() / "meetings" / str(meeting_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
