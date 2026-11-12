"""The single active recording session, tying the Recorder to the Store."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from minutemind import audio_files, paths, store
from minutemind.capture import AudioDevice, Recorder, list_devices

# Pass as the system source to record every output device's sound at once.
ALL_OUTPUTS = "all"

log = logging.getLogger(__name__)

IDLE = "idle"


class RecordingError(RuntimeError):
    """A recording action that can't be done in the current state."""


@dataclass
class _Active:
    meeting: store.Meeting
    recorder: Recorder


class RecordingSession:
    def __init__(
        self,
        db: store.Store,
        recorder_factory: Callable[..., Recorder] = Recorder,
        device_lister: Callable[[], dict[str, list[AudioDevice]]] = list_devices,
    ) -> None:
        self.db = db
        self._recorder_factory = recorder_factory
        self._list_devices = device_lister
        # Called with each meeting that finishes with usable audio (set by the
        # API to queue transcription).
        self.on_finished: Callable[[store.Meeting], None] | None = None
        self._active: _Active | None = None
        self._lock = threading.Lock()

    # -- actions -----------------------------------------------------------

    def start(
        self,
        title: str | None,
        mic_id: int | None,
        system_id: int | str | None,
    ) -> store.Meeting:
        """Start recording. `system_id` is a loopback device id, ALL_OUTPUTS
        to capture every output device, or None for no PC sound."""
        with self._lock:
            if self._active is not None:
                raise RecordingError("A recording is already in progress")
            devices = self._list_devices()
            mic = _pick(devices["inputs"], mic_id) if mic_id is not None else None
            if system_id is None:
                system: list[AudioDevice] = []
            elif system_id == ALL_OUTPUTS:
                system = list(devices["loopbacks"])
            else:
                system = [_pick(devices["loopbacks"], int(system_id))]
            if mic is None and not system:
                raise RecordingError("Choose a microphone, PC sound, or both")

            meeting = self.db.create_meeting(
                title=(title or "").strip() or _default_title(),
                mic_device=mic.name if mic else None,
                system_device=_describe_outputs(system, system_id == ALL_OUTPUTS),
            )
            recorder = self._recorder_factory(paths.meeting_dir(meeting.id), mic, system)
            try:
                recorder.start()
            except Exception as exc:
                self.db.finish_meeting(meeting.id, 0, status=store.FAILED)
                raise RecordingError(f"Couldn't open the audio device: {exc}") from exc
            # Some outputs may have failed to open; record what is really captured.
            opened = recorder.system_devices
            if len(opened) != len(system):
                self.db.set_devices(
                    meeting.id,
                    mic_device=meeting.mic_device,
                    system_device=_describe_outputs(opened, system_id == ALL_OUTPUTS),
                )
                meeting = self.db.get_meeting(meeting.id) or meeting
            self._active = _Active(meeting, recorder)
            return meeting

    def pause(self) -> None:
        with self._lock:
            active = self._require_active()
            active.recorder.pause()
            self.db.set_status(active.meeting.id, store.PAUSED)

    def resume(self) -> None:
        with self._lock:
            active = self._require_active()
            active.recorder.resume()
            self.db.set_status(active.meeting.id, store.RECORDING)

    def stop(self) -> store.Meeting:
        with self._lock:
            active = self._require_active()
            self._active = None
            try:
                duration_ms = active.recorder.stop()
                status = store.COMPLETED
            except Exception:
                log.exception("Stopping the recorder failed")
                duration_ms = active.recorder.clock.elapsed() * 1000
                status = store.FAILED
            meeting = self.db.finish_meeting(active.meeting.id, int(duration_ms), status=status)
        if status == store.COMPLETED:
            self._notify_finished(meeting)
        return meeting

    def shutdown(self) -> None:
        """Called when the service exits: finish any recording cleanly."""
        if self._active is not None:
            try:
                self.stop()
            except Exception:  # noqa: BLE001
                log.exception("Stopping recording during shutdown failed")

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        active = self._active
        if active is None:
            return {"state": IDLE, "meeting": None, "elapsed_ms": 0, "levels": None, "errors": []}
        recorder = active.recorder
        return {
            "state": store.PAUSED if recorder.clock.paused else store.RECORDING,
            "meeting": active.meeting.to_dict(),
            "elapsed_ms": int(recorder.clock.elapsed() * 1000),
            "levels": recorder.levels(),
            "errors": recorder.errors(),
        }

    def is_recording(self, meeting_id: int) -> bool:
        active = self._active
        return active is not None and active.meeting.id == meeting_id

    # -- crash recovery ----------------------------------------------------

    def recover_interrupted(self) -> list[int]:
        """Salvage meetings left 'recording' by a crash or forced quit.

        Repairs the WAV headers, rebuilds the mix, and marks them 'recovered'.
        """
        recovered = []
        for meeting in self.db.active_meetings():
            directory = paths.meeting_dir(meeting.id)
            for part in directory.glob(audio_files.SYSTEM_PART_GLOB):
                audio_files.repair_header(part)
            audio_files.combine_system_parts(directory)
            tracks = [directory / audio_files.MIC_FILE, directory / audio_files.SYSTEM_FILE]
            present = [t for t in tracks if t.exists()]
            for track in present:
                audio_files.repair_header(track)
            mix = audio_files.mix_tracks(directory) if present else None
            duration = audio_files.duration_ms(mix) if mix else 0
            finished = self.db.finish_meeting(
                meeting.id,
                duration,
                status=store.RECOVERED if mix else store.FAILED,
                has_mic=tracks[0].exists(),
                has_system=tracks[1].exists(),
            )
            if mix:
                self._notify_finished(finished)
            recovered.append(meeting.id)
        return recovered

    # -- helpers -----------------------------------------------------------

    def _notify_finished(self, meeting: store.Meeting) -> None:
        if self.on_finished is None:
            return
        try:
            self.on_finished(meeting)
        except Exception:  # noqa: BLE001 - never lose a recording over this
            log.exception("on_finished hook failed for meeting %s", meeting.id)

    def _require_active(self) -> _Active:
        if self._active is None:
            raise RecordingError("Nothing is being recorded")
        return self._active


def _pick(devices: list[AudioDevice], device_id: int) -> AudioDevice:
    for device in devices:
        if device.id == device_id:
            return device
    raise RecordingError("That audio device is no longer available. Refresh the device list.")


def _describe_outputs(devices: list[AudioDevice], all_outputs: bool) -> str | None:
    if not devices:
        return None
    names = ", ".join(d.name for d in devices)
    return f"All outputs ({names})" if all_outputs else names


def _default_title() -> str:
    now = datetime.now()
    return f"Meeting, {now:%d %b %Y} at {now:%H:%M}"
