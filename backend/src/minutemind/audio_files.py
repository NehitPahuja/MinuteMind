"""WAV file helpers: crash repair and mixing the two tracks.

All MinuteMind recordings are 16-bit PCM, mono, 16 kHz.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # bytes, int16
HEADER_SIZE = 44

MIC_FILE = "mic.wav"
SYSTEM_FILE = "system.wav"
MIX_FILE = "mix.wav"
# While recording, each output device's loopback goes to its own part file;
# the parts are combined into SYSTEM_FILE when the recording stops.
SYSTEM_PART_GLOB = "system.part*.wav"


def system_part_file(index: int) -> str:
    return f"system.part{index}.wav"


TRACK_FILES = {"mix": MIX_FILE, "mic": MIC_FILE, "system": SYSTEM_FILE}


def open_writer(path: Path) -> wave.Wave_write:
    writer = wave.open(str(path), "wb")
    writer.setnchannels(1)
    writer.setsampwidth(SAMPLE_WIDTH)
    writer.setframerate(SAMPLE_RATE)
    return writer


def read_samples(path: Path) -> np.ndarray:
    """Read a MinuteMind WAV as int16 samples."""
    with wave.open(str(path), "rb") as reader:
        frames = reader.readframes(reader.getnframes())
    return np.frombuffer(frames, dtype=np.int16)


def duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as reader:
        return int(reader.getnframes() * 1000 / reader.getframerate())


def repair_header(path: Path) -> bool:
    """Fix the RIFF/data sizes of a WAV whose writer never closed (crash, kill).

    Python's wave module only writes the final sizes on close, so an
    interrupted file has the audio but a header claiming zero frames.
    Returns True if the file was changed.
    """
    size = path.stat().st_size
    if size < HEADER_SIZE:
        return False
    data_size = size - HEADER_SIZE
    data_size -= data_size % SAMPLE_WIDTH
    with path.open("r+b") as f:
        header = f.read(HEADER_SIZE)
        if header[0:4] != b"RIFF" or header[36:40] != b"data":
            return False
        riff_size, = struct.unpack("<I", header[4:8])
        current_data, = struct.unpack("<I", header[40:44])
        if riff_size == size - 8 and current_data == data_size:
            return False
        f.seek(4)
        f.write(struct.pack("<I", HEADER_SIZE - 8 + data_size))
        f.seek(40)
        f.write(struct.pack("<I", data_size))
    return True


def sum_files(sources: list[Path], out: Path) -> Path | None:
    """Add WAV files sample by sample (padding shorter ones) into `out`."""
    tracks = [read_samples(p).astype(np.int32) for p in sources if p.exists()]
    if not tracks:
        return None
    length = max(len(t) for t in tracks)
    total = np.zeros(length, dtype=np.int32)
    for track in tracks:
        total[: len(track)] += track
    with open_writer(out) as writer:
        writer.writeframes(np.clip(total, -32768, 32767).astype(np.int16).tobytes())
    return out


def combine_system_parts(directory: Path) -> Path | None:
    """Merge the per-output-device loopback parts into system.wav and delete them."""
    parts = sorted(directory.glob(SYSTEM_PART_GLOB))
    if not parts:
        return None
    out = sum_files(parts, directory / SYSTEM_FILE)
    for part in parts:
        part.unlink(missing_ok=True)
    return out


def mix_tracks(directory: Path) -> Path | None:
    """Combine mic.wav and system.wav into mix.wav. Returns the mix path, or None
    if neither track exists."""
    return sum_files([directory / MIC_FILE, directory / SYSTEM_FILE], directory / MIX_FILE)
