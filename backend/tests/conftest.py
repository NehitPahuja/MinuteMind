from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from minutemind import audio_files, hardware, store
from minutemind.capture import ActiveClock, AudioDevice
from minutemind.transcribe import RawSegment


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


MIC_LEVEL = 1000
PART_LEVEL = 1500  # per output device; two outputs sum to 3000

YOU_SAID = "Good morning team, the launch moves to March."
OTHERS_SAID = "Thanks. The hosting quote is four hundred and twenty dollars."


class FakeEngine:
    """Stands in for WhisperEngine: no model, instant fixed results.

    Quiet audio (the mic in FakeRecorder) "says" YOU_SAID, louder audio says
    OTHERS_SAID. Set fail_on_gpu to simulate the GPU running out of memory.
    """

    model_name = "fake-whisper"

    def __init__(self, fail_on_gpu: bool = False, error: Exception | None = None):
        self.fail_on_gpu = fail_on_gpu
        self.error = error
        self.devices: list[str] = []
        self.downloads = 0
        self.releases = 0

    def ensure_model(self, on_progress) -> None:
        self.downloads += 1
        on_progress(1.0)

    def transcribe(self, audio, profile, on_progress):
        self.devices.append(profile.device)
        if self.error is not None:
            raise self.error
        if self.fail_on_gpu and profile.device == "cuda":
            raise RuntimeError("CUDA failed with error out of memory")
        seconds = len(audio) / audio_files.SAMPLE_RATE
        on_progress(seconds)
        loud = float(np.abs(audio).max()) * 32768 > 2000
        text = OTHERS_SAID if loud else YOU_SAID
        return [RawSegment(0.1, seconds - 0.1, text, [(0.1, 0.4, text.split()[0])])]

    def release(self) -> None:
        self.releases += 1


GPU_PROFILE = hardware.Profile("gpu", "cuda", "int8_float16", "GPU (Test GPU)")
TEST_HARDWARE = hardware.HardwareInfo(
    gpu_name="Test GPU",
    vram_total_mb=4096,
    vram_free_mb=3500,
    cuda_available=True,
    ram_total_mb=16000,
    cpu_threads=8,
)


class FakeHardwareCache:
    def info(self, refresh: bool = False) -> hardware.HardwareInfo:
        return TEST_HARDWARE


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
        # One second per track. Mic and PC sound differ in loudness so the fake
        # speech engine can tell them apart.
        if self.mic is not None:
            with audio_files.open_writer(self.directory / audio_files.MIC_FILE) as w:
                w.writeframes(np.full(audio_files.SAMPLE_RATE, MIC_LEVEL, np.int16).tobytes())
        for index, _device in enumerate(self.system):
            with audio_files.open_writer(self.directory / audio_files.system_part_file(index)) as w:
                w.writeframes(np.full(audio_files.SAMPLE_RATE, PART_LEVEL, np.int16).tobytes())
        audio_files.combine_system_parts(self.directory)
        audio_files.mix_tracks(self.directory)
        return 1000

    def levels(self) -> dict[str, float]:
        return {"mic": 0.5, "system": 0.0}

    def errors(self) -> list[str]:
        return list(self.warnings)
