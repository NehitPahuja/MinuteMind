"""Downloading AI models and keeping at most one of them loaded.

Models live in %APPDATA%\\MinuteMind\\models\\<name>. A 4 GB GPU can't hold the
transcription model and (later) the language model at once, so this manager
loads one at a time and unloads the previous one first.
"""

from __future__ import annotations

import gc
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

from minutemind import paths

log = logging.getLogger(__name__)

# Whisper model used for the final transcript.
TRANSCRIBE_MODEL = "large-v3-turbo"

# Files faster-whisper needs from a model repository.
WHISPER_FILES = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]

ProgressCallback = Callable[[float], None]

DOWNLOAD_ATTEMPTS = 6


def models_dir() -> Path:
    path = paths.data_dir() / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def whisper_repo(name: str) -> str:
    from faster_whisper.utils import _MODELS

    return _MODELS.get(name, name)


def is_downloaded(name: str) -> bool:
    folder = models_dir() / name
    return (folder / "model.bin").exists() and (folder / "config.json").exists()


def download_whisper(name: str, on_progress: ProgressCallback | None = None) -> Path:
    """Download a Whisper model if needed and return its folder.

    Progress (0..1) is reported by watching the bytes arriving on disk, since
    huggingface_hub has no simple progress hook.
    """
    target = models_dir() / name
    if is_downloaded(name):
        return target

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from huggingface_hub import HfApi, snapshot_download

    repo = whisper_repo(name)
    staging = models_dir() / f".{name}.downloading"
    total = _expected_bytes(HfApi(), repo)

    done = threading.Event()
    error: list[BaseException] = []

    def fetch() -> None:
        # Slow or flaky connections stall; huggingface_hub keeps partial files,
        # so each retry resumes where the last attempt stopped.
        try:
            for attempt in range(DOWNLOAD_ATTEMPTS):
                try:
                    snapshot_download(repo, local_dir=staging, allow_patterns=WHISPER_FILES)
                    return
                except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
                    log.warning("Download of %s failed (attempt %d): %s", name, attempt + 1, exc)
                    if attempt + 1 == DOWNLOAD_ATTEMPTS:
                        error.append(exc)
                    else:
                        time.sleep(min(30, 2 ** attempt))
        finally:
            done.set()

    threading.Thread(target=fetch, name=f"download-{name}", daemon=True).start()
    while not done.wait(0.5):
        if on_progress and total:
            on_progress(min(0.99, _folder_bytes(staging) / total))
    if error:
        raise RuntimeError(f"Downloading {name} failed: {error[0]}") from error[0]

    shutil.rmtree(staging / ".cache", ignore_errors=True)
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    if on_progress:
        on_progress(1.0)
    return target


def _expected_bytes(api: Any, repo: str) -> int:
    import fnmatch

    try:
        info = api.model_info(repo, files_metadata=True)
    except Exception:  # noqa: BLE001 - progress is optional
        return 0
    return sum(
        (f.size or 0)
        for f in info.siblings or []
        if any(fnmatch.fnmatch(f.rfilename, pattern) for pattern in WHISPER_FILES)
    )


def _folder_bytes(folder: Path) -> int:
    if not folder.exists():
        return 0
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


class ModelManager:
    """Holds at most one loaded model. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._key: tuple | None = None
        self._model: Any = None

    @property
    def loaded(self) -> tuple | None:
        return self._key

    def whisper(self, name: str, device: str, compute_type: str) -> Any:
        """Return a loaded faster-whisper model, unloading anything else first."""
        key = ("whisper", name, device, compute_type)
        with self._lock:
            if self._key == key:
                return self._model
            self.unload()
            from minutemind import gpu_env

            gpu_env.prepare()
            from faster_whisper import WhisperModel

            started = time.monotonic()
            self._model = WhisperModel(
                str(models_dir() / name),
                device=device,
                compute_type=compute_type,
                cpu_threads=max(1, (os.cpu_count() or 2) - 1),
            )
            self._key = key
            log.info("Loaded %s on %s in %.1fs", name, device, time.monotonic() - started)
            return self._model

    def unload(self) -> None:
        with self._lock:
            if self._model is None:
                return
            log.info("Unloading %s", self._key)
            self._model = None
            self._key = None
            gc.collect()
