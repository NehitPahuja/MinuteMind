r"""Build a test meeting from TTS clips and run the real transcription + speakers + minutes job.

Usage, from backend/:
    powershell -File scripts/make_clips.ps1 -Out <clips dir>
    MINUTEMIND_DATA_DIR=<scratch folder> .venv/Scripts/python.exe scripts/run_pipeline.py <clips dir>
Link <scratch folder>\models to the real models folder first (mklink /J), or it downloads them again.
"""

import json
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

from minutemind import gpu_env

gpu_env.prepare()

from minutemind import audio_files, diarize, hardware, minutes, models, paths, store  # noqa: E402
from minutemind.llm.llamacpp import LlamaCppModel  # noqa: E402
from minutemind.transcribe import TranscriptionQueue, WhisperEngine  # noqa: E402

SR = 16000
# clip -> (track, start seconds); 01/04/07 are "You" on the mic, the rest PC sound.
LAYOUT = {
    "01": ("mic", 0.5),
    "02": ("system", 9.5),
    "03": ("system", 17.5),
    "04": ("mic", 27.5),
    "05": ("system", 38.0),
    "06": ("system", 45.0),
    "07": ("mic", 50.0),
}
NOTE = (33_000, "Ads budget of $5,000 approved by finance")


def read(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def write(path: Path, data: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.astype(np.int16).tobytes())


def gpu_watch(stop: threading.Event, samples: list) -> None:
    while not stop.is_set():
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True,
        ).stdout.strip()
        samples.append((round(time.time() - T0, 1), int(out or 0)))
        time.sleep(0.5)


def main() -> None:
    clips = Path(sys.argv[1])
    db = store.Store()
    meeting = db.create_meeting("Mobile app launch sync", "test mic", "test system")
    folder = paths.meeting_dir(meeting.id)
    tracks = {"mic": [], "system": []}
    for name, (track, start) in LAYOUT.items():
        tracks[track].append((int(start * SR), read(clips / f"{name}.wav")))
    length = max(o + len(d) for t in tracks.values() for o, d in t) + SR
    out = {}
    for track, parts in tracks.items():
        buf = np.zeros(length, dtype=np.int32)
        for offset, data in parts:
            buf[offset : offset + len(data)] += data
        out[track] = buf
    write(folder / audio_files.MIC_FILE, out["mic"])
    write(folder / audio_files.SYSTEM_FILE, out["system"])
    write(folder / audio_files.MIX_FILE, np.clip(out["mic"] + out["system"], -32768, 32767))
    db.finish_meeting(meeting.id, int(length / SR * 1000))
    db.add_note(meeting.id, *NOTE)

    info = hardware.detect()
    manager = models.ModelManager()
    queue = TranscriptionQueue(
        db,
        WhisperEngine(manager),
        lambda: hardware.choose_profile(info, "auto"),
        detector=diarize.SherpaDetector(),
        writer=minutes.MinutesWriter(LlamaCppModel(manager)),
    )
    stop, samples = threading.Event(), []
    watcher = threading.Thread(target=gpu_watch, args=(stop, samples), daemon=True)
    watcher.start()
    queue.start()
    started = time.time()
    queue.enqueue(meeting.id)
    queue.wait_idle(timeout=900)
    print(f"job took {time.time() - started:.1f} s")
    time.sleep(1.5)
    stop.set()

    m = db.get_meeting(meeting.id)
    print("statuses:", m.to_dict().get("transcript_status"), m.to_dict().get("speakers_status"),
          m.to_dict().get("minutes_status"), m.to_dict().get("minutes_error"))
    print("\nTRANSCRIPT")
    for s in db.get_segments(meeting.id):
        print(f"  {s.start_ms/1000:5.1f} {s.speaker or s.source}: {s.text}")
    print("\nMINUTES")
    print(json.dumps(db.get_minutes(meeting.id), indent=2))
    # Compress GPU samples to the points where usage changed.
    print("\nGPU MiB (t, used) at changes:")
    last = None
    for t, mib in samples:
        if last is None or abs(mib - last) > 60:
            print(f"  {t:6.1f}s {mib}")
            last = mib


T0 = time.time()
if __name__ == "__main__":
    main()
