from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from minutemind import audio_files


def _write(path: Path, samples: np.ndarray) -> None:
    with audio_files.open_writer(path) as w:
        w.writeframes(samples.astype(np.int16).tobytes())


def test_repair_header_restores_duration(tmp_path: Path) -> None:
    path = tmp_path / "mic.wav"
    _write(path, np.ones(audio_files.SAMPLE_RATE * 2))
    # Simulate a crash before the writer patched its header: sizes left at zero.
    with path.open("r+b") as f:
        f.seek(4)
        f.write(struct.pack("<I", 36))
        f.seek(40)
        f.write(struct.pack("<I", 0))
    assert audio_files.duration_ms(path) == 0

    assert audio_files.repair_header(path) is True
    assert audio_files.duration_ms(path) == 2000
    assert audio_files.repair_header(path) is False  # already fine


def test_mix_sums_and_pads_tracks(tmp_path: Path) -> None:
    _write(tmp_path / audio_files.MIC_FILE, np.full(100, 1000))
    _write(tmp_path / audio_files.SYSTEM_FILE, np.full(150, 30000))
    mix = audio_files.mix_tracks(tmp_path)
    assert mix is not None
    samples = audio_files.read_samples(mix)
    assert len(samples) == 150
    assert samples[0] == 31000
    assert samples[120] == 30000
    # Loud overlaps clip instead of wrapping around.
    _write(tmp_path / audio_files.MIC_FILE, np.full(100, 30000))
    assert audio_files.read_samples(audio_files.mix_tracks(tmp_path))[0] == 32767


def test_mix_with_one_track(tmp_path: Path) -> None:
    _write(tmp_path / audio_files.MIC_FILE, np.full(50, 7))
    mix = audio_files.mix_tracks(tmp_path)
    assert mix is not None and len(audio_files.read_samples(mix)) == 50


def test_mix_with_no_tracks(tmp_path: Path) -> None:
    assert audio_files.mix_tracks(tmp_path) is None


def test_combine_system_parts(tmp_path: Path) -> None:
    _write(tmp_path / audio_files.system_part_file(0), np.full(80, 10))
    _write(tmp_path / audio_files.system_part_file(1), np.full(100, 5))
    out = audio_files.combine_system_parts(tmp_path)
    assert out == tmp_path / audio_files.SYSTEM_FILE
    samples = audio_files.read_samples(out)
    assert len(samples) == 100 and samples[0] == 15 and samples[90] == 5
    assert not list(tmp_path.glob(audio_files.SYSTEM_PART_GLOB))
    assert audio_files.combine_system_parts(tmp_path) is None  # nothing left to combine
