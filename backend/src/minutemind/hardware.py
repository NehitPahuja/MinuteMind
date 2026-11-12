"""Detect the GPU and memory, and choose how heavy work (transcription) runs.

Profiles (see docs/PLAN.md):
  gpu        NVIDIA GPU with ~4 GB or more: everything on the GPU
  small_gpu  NVIDIA GPU with 2-4 GB: transcription still fits on the GPU
  cpu        no usable NVIDIA GPU: everything on the processor (slower)
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import asdict, dataclass

import psutil

log = logging.getLogger(__name__)

GPU_MIN_MB = 3800  # a "4 GB" card reports 4096 MiB total
SMALL_GPU_MIN_MB = 2000


@dataclass(frozen=True)
class HardwareInfo:
    gpu_name: str | None
    vram_total_mb: int | None
    vram_free_mb: int | None
    cuda_available: bool
    ram_total_mb: int
    cpu_threads: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Profile:
    name: str  # "gpu" | "small_gpu" | "cpu"
    device: str  # "cuda" | "cpu"
    compute_type: str  # CTranslate2 compute type
    label: str  # shown in Settings

    def to_dict(self) -> dict:
        return asdict(self)


CPU_PROFILE = Profile("cpu", "cpu", "int8", "Processor (no GPU)")


def detect() -> HardwareInfo:
    name, total, free = _query_nvidia_smi()
    return HardwareInfo(
        gpu_name=name,
        vram_total_mb=total,
        vram_free_mb=free,
        cuda_available=name is not None and _cuda_device_count() > 0,
        ram_total_mb=int(psutil.virtual_memory().total / 2**20),
        cpu_threads=os.cpu_count() or 1,
    )


def choose_profile(info: HardwareInfo, preference: str = "auto") -> Profile:
    """Pick a profile. `preference` is the user's setting: auto, gpu or cpu."""
    if preference == "cpu" or not info.cuda_available:
        return CPU_PROFILE
    vram = info.vram_total_mb or 0
    if vram >= GPU_MIN_MB or preference == "gpu":
        return Profile("gpu", "cuda", "int8_float16", f"GPU ({info.gpu_name})")
    if vram >= SMALL_GPU_MIN_MB:
        return Profile("small_gpu", "cuda", "int8_float16", f"Small GPU ({info.gpu_name})")
    return CPU_PROFILE


def _query_nvidia_smi() -> tuple[str | None, int | None, int | None]:
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None, None, None
    if out.returncode != 0 or not out.stdout.strip():
        return None, None, None
    first = out.stdout.strip().splitlines()[0]
    try:
        name, total, free = (part.strip() for part in first.split(","))
        return name, int(float(total)), int(float(free))
    except ValueError:
        log.warning("Unexpected nvidia-smi output: %r", first)
        return None, None, None


def _cuda_device_count() -> int:
    try:
        from minutemind import gpu_env

        gpu_env.prepare()
        import ctranslate2

        return ctranslate2.get_cuda_device_count()
    except Exception:  # noqa: BLE001 - no CUDA runtime is a normal situation
        return 0
