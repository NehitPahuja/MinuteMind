"""Make the pip-installed NVIDIA libraries (cuBLAS, cuDNN) loadable on Windows.

CTranslate2 (used by faster-whisper) loads cublas64_12.dll and cudnn*.dll at
runtime. The `nvidia-*-cu12` wheels put them in site-packages/nvidia/*/bin,
which isn't on the DLL search path, so they are added here. Call
`prepare()` before importing faster_whisper. Safe to call more than once.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_done = False


def nvidia_bin_dirs() -> list[Path]:
    dirs: list[Path] = []
    for entry in sys.path:
        root = Path(entry) / "nvidia"
        if root.is_dir():
            dirs.extend(sorted(p for p in root.glob("*/bin") if p.is_dir()))
    return dirs


def prepare() -> None:
    global _done
    if _done or os.name != "nt":
        _done = True
        return
    for directory in nvidia_bin_dirs():
        os.add_dll_directory(str(directory))
        # Some loaders still consult PATH rather than the added directories.
        os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
    _done = True
