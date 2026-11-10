"""Tests that need real Windows audio devices. Skipped where none are available."""

from __future__ import annotations

import threading

import pytest

from minutemind.capture import list_devices


def _devices_or_skip():
    try:
        devices = list_devices()
    except Exception as exc:  # noqa: BLE001 - no PortAudio/WASAPI here
        pytest.skip(f"No audio hardware: {exc}")
    if not devices["inputs"] and not devices["loopbacks"]:
        pytest.skip("No audio devices")
    return devices


def test_concurrent_device_listing_does_not_crash() -> None:
    """Regression: two requests listing devices at once crashed the process
    (access violation in PortAudio). A crash here kills pytest outright."""
    _devices_or_skip()
    errors: list[str] = []

    def worker() -> None:
        for _ in range(5):
            try:
                list_devices()
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
