from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from minutemind import audio_files, store
from minutemind.capture import ActiveClock, AudioDevice


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own empty MinuteMind data folder."""
    monkeypatch.setenv("MINUTEMIND_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def db() -> store.Store:
    return store.Store()


MIC = AudioDevice(id=1, name="Test Mic", channels=1, rate=48_000, is_default=True)
LOOPBACK = AudioDevice(id=2, name="Test Speakers", channels=2, rate=44_100, is_default=True)
HEADPHONES = AudioDevice(id=3, name="Test Headphones", channels=2, rate=48_000, is_default=False)


def fake_devices() -> dict[str, list[AudioDevice]]:
    return {"inputs": [MIC], "loopbacks": [LOOPBACK, HEADPHONES]}


class FakeRecorder:
    """Stands in for capture.Recorder: writes short tracks instead of using hardware."""

    # Device names that "fail to open", to test partial starts.
    broken: set[str] = set()

    def __init__(self, directory: Path, mic: AudioDevice | None, system: list[AudioDevice]):
        self.directory = directory
        self.mic = mic
        self.system = [d for d in system if d.name not in self.broken]
        self.warnings = [f"Couldn't record {d.name}" for d in system if d.name in self.broken]
        self.clock = ActiveClock()
        self.started = False
        self.stopped = False

    @property
    def system_devices(self) -> list[AudioDevice]:
        return self.system

    def start(self) -> None:
        self.started = True
        self.clock.start()

    def pause(self) -> None:
        self.clock.pause()

    def resume(self) -> None:
        self.clock.resume()

    def stop(self) -> int:
        self.stopped = True
        samples = np.full(audio_files.SAMPLE_RATE, 1000, dtype=np.int16)  # 1 second
        if self.mic is not None:
            with audio_files.open_writer(self.directory / audio_files.MIC_FILE) as w:
                w.writeframes(samples.tobytes())
        for index, _device in enumerate(self.system):
            with audio_files.open_writer(self.directory / audio_files.system_part_file(index)) as w:
                w.writeframes(samples.tobytes())
        audio_files.combine_system_parts(self.directory)
        audio_files.mix_tracks(self.directory)
        return 1000

    def levels(self) -> dict[str, float]:
        return {"mic": 0.5, "system": 0.0}

    def errors(self) -> list[str]:
        return list(self.warnings)
