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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class LlmSpec:
    """A GGUF language model on Hugging Face."""

    name: str  # shown in the UI
    repo: str
    filename: str
    context_tokens: int


# Language model for meeting minutes (about 2 GB). Qwen 2.5 3B Instruct follows
# instructions and JSON schemas well for its size and fits a 4 GB GPU.
MINUTES_LLM = LlmSpec(
    name="qwen2.5-3b-instruct",
    repo="Qwen/Qwen2.5-3B-Instruct-GGUF",
    filename="qwen2.5-3b-instruct-q4_k_m.gguf",
    context_tokens=8192,
)

# A much smaller model (about 0.5 GB) for testing on slow connections. Its
# minutes are noticeably worse; select it with MINUTEMIND_LLM=qwen2.5-0.5b-instruct.
SMALL_LLM = LlmSpec(
    name="qwen2.5-0.5b-instruct",
    repo="Qwen/Qwen2.5-0.5B-Instruct-GGUF",
    filename="qwen2.5-0.5b-instruct-q4_k_m.gguf",
    context_tokens=8192,
)

LLM_SPECS = {spec.name: spec for spec in (MINUTES_LLM, SMALL_LLM)}

LLM_DIR = "llm"


def minutes_llm() -> LlmSpec:
    """The language model for minutes; MINUTEMIND_LLM overrides it."""
    override = os.environ.get("MINUTEMIND_LLM", "").strip()
    if override:
        if override in LLM_SPECS:
            return LLM_SPECS[override]
        log.warning("Unknown MINUTEMIND_LLM %r; using %s", override, MINUTES_LLM.name)
    return MINUTES_LLM


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
    # Slow or flaky connections stall; huggingface_hub keeps partial files,
    # so each retry resumes where the last attempt stopped.
    _fetch_with_retries(
        name,
        lambda: snapshot_download(repo, local_dir=staging, allow_patterns=WHISPER_FILES),
        staging,
        total,
        on_progress,
    )

    shutil.rmtree(staging / ".cache", ignore_errors=True)
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    if on_progress:
        on_progress(1.0)
    return target


def llm_path(spec: LlmSpec) -> Path:
    return models_dir() / LLM_DIR / spec.filename


def is_llm_downloaded(spec: LlmSpec) -> bool:
    return llm_path(spec).exists()


def download_llm(spec: LlmSpec, on_progress: ProgressCallback | None = None) -> Path:
    """Download a GGUF language model if needed and return its path."""
    target = llm_path(spec)
    if target.exists():
        return target

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from huggingface_hub import HfApi, hf_hub_download

    staging = models_dir() / f".{spec.name}.downloading"
    total = _expected_bytes(HfApi(), spec.repo, [spec.filename])
    _fetch_with_retries(
        spec.name,
        lambda: hf_hub_download(spec.repo, spec.filename, local_dir=staging),
        staging,
        total,
        on_progress,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    (staging / spec.filename).replace(target)
    shutil.rmtree(staging, ignore_errors=True)
    if on_progress:
        on_progress(1.0)
    return target


def _fetch_with_retries(
    name: str,
    fetch: Callable[[], object],
    staging: Path,
    total: int,
    on_progress: ProgressCallback | None,
) -> None:
    """Run `fetch` on a thread, retrying on errors (huggingface_hub resumes
    partial files) and reporting progress from the bytes in `staging`."""
    done = threading.Event()
    error: list[BaseException] = []

    def run() -> None:
        try:
            for attempt in range(DOWNLOAD_ATTEMPTS):
                try:
                    fetch()
                    return
                except Exception as exc:  # noqa: BLE001 - retried, then re-raised below
                    log.warning("Download of %s failed (attempt %d): %s", name, attempt + 1, exc)
                    if attempt + 1 == DOWNLOAD_ATTEMPTS:
                        error.append(exc)
                    else:
                        time.sleep(min(30, 2 ** attempt))
        finally:
            done.set()

    threading.Thread(target=run, name=f"download-{name}", daemon=True).start()
    while not done.wait(0.5):
        if on_progress and total:
            on_progress(min(0.99, _folder_bytes(staging) / total))
    if error:
        raise RuntimeError(f"Downloading {name} failed: {error[0]}") from error[0]


def _expected_bytes(api: Any, repo: str, patterns: list[str] = WHISPER_FILES) -> int:
    import fnmatch

    try:
        info = api.model_info(repo, files_metadata=True)
    except Exception:  # noqa: BLE001 - progress is optional
        return 0
    return sum(
        (f.size or 0)
        for f in info.siblings or []
        if any(fnmatch.fnmatch(f.rfilename, pattern) for pattern in patterns)
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

    def llama(self, spec: LlmSpec, gpu_layers: int) -> Any:
        """Return a loaded llama.cpp model, unloading anything else first."""
        key = ("llama", spec.name, gpu_layers)
        with self._lock:
            if self._key == key:
                return self._model
            self.unload()
            from minutemind import gpu_env

            gpu_env.prepare()
            from llama_cpp import Llama

            started = time.monotonic()
            self._model = Llama(
                model_path=str(llm_path(spec)),
                n_gpu_layers=gpu_layers,
                n_ctx=spec.context_tokens,
                n_threads=max(1, (os.cpu_count() or 2) // 2),
                verbose=False,
            )
            self._key = key
            log.info(
                "Loaded %s (%s GPU layers) in %.1fs",
                spec.name,
                "all" if gpu_layers < 0 else gpu_layers,
                time.monotonic() - started,
            )
            return self._model

    def unload(self) -> None:
        with self._lock:
            if self._model is None:
                return
            log.info("Unloading %s", self._key)
            close = getattr(self._model, "close", None)
            if callable(close):
                # llama.cpp frees its GPU memory here, not when garbage collected.
                try:
                    close()
                except Exception:  # noqa: BLE001
                    log.exception("Closing %s failed", self._key)
            self._model = None
            self._key = None
            gc.collect()
