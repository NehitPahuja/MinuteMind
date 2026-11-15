"""Build the self-contained Python runtime that the Windows installer ships.

Copies uv's standalone (relocatable) CPython 3.12 that backend/.venv is based
on, adds the venv's installed packages, and the minutemind package itself as
plain files, into app/build-python/python. electron-builder then ships that
folder as resources/python (see app/electron-builder.yml).

Files a packaged app never uses are left out to keep the installer small:
headers, link libraries, test suites, Tk, pip, caches, and site-packages/bin
(a second copy of llama.cpp's 700 MB of DLLs, which llama_cpp/lib already has).

Usage, from backend/ (after `uv sync`):
    .venv/Scripts/python.exe scripts/stage_runtime.py
"""

from __future__ import annotations

import configparser
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
VENV = BACKEND / ".venv"
SITE = VENV / "Lib" / "site-packages"
OUT = BACKEND.parent / "app" / "build-python" / "python"

# Relative to the base interpreter folder.
BASE_SKIP = {"Lib/test", "Lib/idlelib", "Lib/tkinter", "Lib/turtledemo", "tcl", "include", "libs",
             "Lib/site-packages", "Scripts"}
# Top-level entries of site-packages.
SITE_SKIP = {"bin", "_virtualenv.pth", "_virtualenv.py", "minutemind.pth", "pip", "__pycache__"}
SITE_SKIP_PREFIX = ("minutemind-", "pip-")
# Files dropped anywhere: headers, link libraries, compiled caches.
DROP_SUFFIXES = (".lib", ".h", ".hpp", ".cuh", ".pyc", ".a")
DROP_DIRS = {"__pycache__", "include", "tests", "test"}
# llama.cpp command-line tools and scripts the app never runs.
DROP_NAMES = {"llama-llava-cli.exe", "llama-minicpmv-cli.exe", "convert_hf_to_gguf.py"}


def base_python() -> Path:
    cfg = configparser.ConfigParser()
    cfg.read_string("[venv]\n" + (VENV / "pyvenv.cfg").read_text())
    return Path(cfg["venv"]["home"])


def copy_tree(src: Path, dst: Path, skip) -> int:
    """Copy files under src to dst, skipping what `skip(relative_path)` or the
    drop lists exclude. Returns the bytes copied."""
    total = 0
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if path.is_dir() or skip(rel):
            continue
        if path.suffix.lower() in DROP_SUFFIXES or path.name in DROP_NAMES:
            continue
        if any(part in DROP_DIRS for part in rel.parts[:-1]):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        total += path.stat().st_size
    return total


def main() -> int:
    base = base_python()
    if not (base / "python.exe").exists():
        print(f"Base interpreter not found at {base}", file=sys.stderr)
        return 1
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    def skip_base(rel: Path) -> bool:
        text = rel.as_posix()
        return any(text == s or text.startswith(s + "/") for s in BASE_SKIP)

    size = copy_tree(base, OUT, skip_base)

    site_out = OUT / "Lib" / "site-packages"

    def skip_site(rel: Path) -> bool:
        top = rel.parts[0]
        return top in SITE_SKIP or top.startswith(SITE_SKIP_PREFIX)

    size += copy_tree(SITE, site_out, skip_site)

    # minutemind itself, as a normal package instead of the venv's editable link.
    size += copy_tree(BACKEND / "src" / "minutemind", site_out / "minutemind", lambda rel: False)

    print(f"Staged Python runtime at {OUT} ({size / 1e9:.2f} GB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
