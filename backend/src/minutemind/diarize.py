"""Telling apart the people on the other side of a call ("speaker diarization").

Uses sherpa-onnx: the pyannote segmentation-3.0 model (exported to ONNX) finds
who-speaks-when, a WeSpeaker ResNet34 model (VoxCeleb, English) turns each
stretch into a voice fingerprint, and clustering groups fingerprints into
speakers. It runs on the processor through ONNX Runtime, needs no PyTorch and
no Hugging Face token, and the two models are ~33 MB from GitHub.

Only the PC-sound track is diarized: the microphone track is already "you".
Each transcribed word is then given to the speaker talking at that moment, and
lines are split wherever the speaker changes.
"""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from minutemind import audio_files, models, store

log = logging.getLogger(__name__)

SR = audio_files.SAMPLE_RATE

_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_URL = (
    f"{_RELEASES}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
EMBEDDING_URL = (
    f"{_RELEASES}/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"
)
SEGMENTATION_FILE = "segmentation.onnx"
EMBEDDING_FILE = "embedding.onnx"
DOWNLOAD_ATTEMPTS = 6

# Lower splits voices more readily. 0.3 separated three synthetic voices
# (two of them similar female voices) that 0.4 merged; 0.2 over-split.
CLUSTER_THRESHOLD = 0.3


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    speaker: int  # cluster number from the model


class Detector(Protocol):
    def ensure_models(self, on_progress: Callable[[float], None]) -> None: ...

    def detect(self, audio: np.ndarray, num_speakers: int | None) -> list[Turn]: ...


def models_folder() -> Path:
    path = models.models_dir() / "diarization"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_downloaded() -> bool:
    folder = models_folder()
    return (folder / SEGMENTATION_FILE).exists() and (folder / EMBEDDING_FILE).exists()


class SherpaDetector:
    def __init__(self, threads: int | None = None) -> None:
        self.threads = threads or max(1, min(8, (os.cpu_count() or 2) - 1))
        self._lock = threading.Lock()
        self._cache: dict[int, object] = {}

    def ensure_models(self, on_progress: Callable[[float], None]) -> None:
        if is_downloaded():
            return
        folder = models_folder()
        # Rough sizes, for progress across both files.
        seg_bytes, emb_bytes = 7_000_000, 26_500_000
        total = seg_bytes + emb_bytes

        archive = folder / "segmentation.tar.bz2"
        _download(SEGMENTATION_URL, archive, lambda n: on_progress(min(0.99, n / total)))
        with tarfile.open(archive, "r:bz2") as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("/model.onnx"))
            source = tar.extractfile(member)
            assert source is not None
            with open(folder / SEGMENTATION_FILE, "wb") as out:
                shutil.copyfileobj(source, out)
        archive.unlink(missing_ok=True)

        _download(
            EMBEDDING_URL,
            folder / EMBEDDING_FILE,
            lambda n: on_progress(min(0.99, (seg_bytes + n) / total)),
        )
        on_progress(1.0)

    def detect(self, audio: np.ndarray, num_speakers: int | None) -> list[Turn]:
        diarizer = self._diarizer(num_speakers or -1)
        result = diarizer.process(audio.astype(np.float32, copy=False)).sort_by_start_time()
        return [Turn(r.start, r.end, r.speaker) for r in result]

    def _diarizer(self, num_clusters: int):
        import sherpa_onnx as so

        with self._lock:
            if num_clusters not in self._cache:
                folder = models_folder()
                config = so.OfflineSpeakerDiarizationConfig(
                    segmentation=so.OfflineSpeakerSegmentationModelConfig(
                        pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(
                            model=str(folder / SEGMENTATION_FILE)
                        ),
                        num_threads=self.threads,
                    ),
                    embedding=so.SpeakerEmbeddingExtractorConfig(
                        model=str(folder / EMBEDDING_FILE), num_threads=self.threads
                    ),
                    clustering=so.FastClusteringConfig(
                        num_clusters=num_clusters, threshold=CLUSTER_THRESHOLD
                    ),
                    min_duration_on=0.3,
                    min_duration_off=0.5,
                )
                if not config.validate():
                    raise RuntimeError("Speaker detection models are missing or invalid")
                self._cache[num_clusters] = so.OfflineSpeakerDiarization(config)
            return self._cache[num_clusters]


def _download(url: str, target: Path, on_bytes: Callable[[int], None]) -> None:
    """Download with retries; resumes a partial file when the server allows."""
    partial = target.with_suffix(target.suffix + ".part")
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            have = partial.stat().st_size if partial.exists() else 0
            request = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
            with urllib.request.urlopen(request, timeout=60) as response:
                mode = "ab" if have and response.status == 206 else "wb"
                done = have if mode == "ab" else 0
                with open(partial, mode) as out:
                    while chunk := response.read(256 * 1024):
                        out.write(chunk)
                        done += len(chunk)
                        on_bytes(done)
            partial.replace(target)
            return
        except Exception as exc:  # noqa: BLE001 - retried, then re-raised
            log.warning("Downloading %s failed (attempt %d): %s", url, attempt + 1, exc)
            if attempt + 1 == DOWNLOAD_ATTEMPTS:
                raise RuntimeError(f"Couldn't download the speaker models: {exc}") from exc
            time.sleep(min(30, 2**attempt))


# ---------------------------------------------------------------------------
# Giving transcript lines to speakers


def label_for(index: int) -> str:
    return f"S{index + 1}"


def assign_speakers(segments: list[store.Segment], turns: list[Turn]) -> list[store.Segment]:
    """Give every PC-sound line a speaker, splitting lines where the speaker
    changes mid-line (using word timings). Microphone lines are left as they
    are: they're "you". Speakers are numbered in order of first appearance."""
    if not turns:
        return segments

    order: dict[int, str] = {}

    def label(cluster: int) -> str:
        if cluster not in order:
            order[cluster] = label_for(len(order))
        return order[cluster]

    out: list[store.Segment] = []
    for seg in segments:
        if seg.source != "system":
            out.append(seg)
            continue
        if not seg.words:
            cluster = _speaker_at(turns, seg.start_ms / 1000, seg.end_ms / 1000)
            out.append(_with_speaker(seg, label(cluster)))
            continue
        # Group consecutive words by speaker.
        groups: list[tuple[str, list[tuple[int, int, str]]]] = []
        for word in seg.words:
            start_ms, end_ms, _text = word
            who = label(_speaker_at(turns, start_ms / 1000, end_ms / 1000))
            if groups and groups[-1][0] == who:
                groups[-1][1].append(word)
            else:
                groups.append((who, [word]))
        if len(groups) == 1:
            # One speaker for the whole line: keep its text and timing exactly.
            out.append(_with_speaker(seg, groups[0][0]))
            continue
        for who, words in groups:
            text = "".join(w[2] for w in words).strip()
            if not text:
                continue
            out.append(
                store.Segment(
                    source="system",
                    start_ms=words[0][0],
                    end_ms=words[-1][1],
                    text=text,
                    words=words,
                    speaker=who,
                )
            )
    return sorted(out, key=lambda s: (s.start_ms, s.source))


def _speaker_at(turns: list[Turn], start: float, end: float) -> int:
    """The speaker overlapping [start, end] the most, else the nearest one."""
    best, best_overlap = None, 0.0
    for turn in turns:
        overlap = min(end, turn.end) - max(start, turn.start)
        if overlap > best_overlap:
            best, best_overlap = turn.speaker, overlap
    if best is not None:
        return best
    # No overlap (e.g. a word in a gap the model treated as silence): use the
    # nearest stretch of speech.
    mid = (start + end) / 2
    nearest = min(turns, key=lambda t: min(abs(mid - t.start), abs(mid - t.end)))
    return nearest.speaker


def _with_speaker(seg: store.Segment, speaker: str) -> store.Segment:
    return store.Segment(
        source=seg.source,
        start_ms=seg.start_ms,
        end_ms=seg.end_ms,
        text=seg.text,
        words=seg.words,
        speaker=speaker,
        id=seg.id,
    )
