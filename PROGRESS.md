# MinuteMind progress

Read this first in every new session, then [docs/PLAN.md](docs/PLAN.md). Update it at the end of each session.

## Phase status

| Phase | Name | Status |
|---|---|---|
| 0 | Foundations | Done |
| 1 | Recording | Done (see the open check below) |
| 2 | Transcription after the meeting | Not started |
| 3 | Live transcript and notes | Not started |
| 4 | Speaker labels | Not started |
| 5 | Local AI and meeting minutes | Not started |
| 6 | Search and Ask | Not started |
| 7 | Claude option | Not started |
| 8 | Polish and installer | Not started |

### Phase 1 open check

The plan's "done when" is a 30-second recording with a YouTube clip playing while you speak. Everything except a human doing that was verified:

- Real mic and loopback devices record, pause and stop, with all three tracks the same length.
- Real PC sound is captured: a 440 Hz tone played into the silent Voice Changer virtual device landed at the right time and pitch, with silence padded before and after.
- The full UI flow (start, live view, stop, list, play back) and quitting mid-recording were run in the real app.

Still to do by hand: record a real call or a YouTube clip with the AirPods or speakers and listen back to the You, Others and Everyone tracks.

## Next up: Phase 2, Transcription after the meeting

1. Add `faster-whisper` (CTranslate2) to the backend, plus `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` for GPU, and check they load on Python 3.12.
2. `hardware.py`: detect the NVIDIA GPU and free VRAM (CTranslate2's CUDA device count, or `nvidia-smi`) and total RAM (`psutil`), then pick the GPU, Small GPU or CPU profile from the plan.
3. `models.py`: download models into `%APPDATA%\MinuteMind\models\` with progress, and load or unload them so only one uses the GPU at a time.
4. `transcribe.py`: a background job that transcribes `mix.wav` with `large-v3-turbo` and word timestamps, reports progress, and retries on CPU if the GPU runs out of memory. Transcribing `mic.wav` and `system.wav` separately may label speakers more reliably in Phase 4, so decide which to use first.
5. Add a `segments` table (migration 2) and `/meetings/{id}/transcript` plus job-status routes, and push job progress over the existing `/ws`.
6. UI: a transcript view under each meeting where clicking a line seeks the `AudioPlayer`, a progress bar while transcribing, and the hardware profile in Settings.

## Decisions made

- The backend uses a **src layout**: code lives in `backend/src/minutemind/`, not `backend/minutemind/` as first sketched in the plan.
- **Port handshake:** the backend binds port 0 and prints `MINUTEMIND_PORT=<port>` on stdout. Electron reads that line, then polls `/health`. This avoids racing for a free port.
- **Auth:** every request needs the per-launch token, passed to the backend through the `MINUTEMIND_TOKEN` environment variable so it stays out of process listings. Clients send it as the `X-MinuteMind-Token` header, or as a `token` query parameter where headers aren't possible (`<audio>` and WebSockets).
- **No orphans:** on quit, Electron first asks the backend to stop any recording (`BackendManager.shutdown`), then kills the process tree with `taskkill /T /F`. The backend watches Electron's PID and exits within about 2 seconds if Electron dies.
- **Dev launch:** Electron runs `backend/.venv/Scripts/python.exe` directly and falls back to `uv run` if the venv doesn't exist yet. On Windows that venv `python.exe` is a launcher, so the real interpreter is its child process.
- **Renderer to backend:** `BackendProvider` fetches `{ baseUrl, token }` from `window.api.backend.getConnection()` and exposes a `BackendClient` through `useBackend()`. A restarted backend gets a new port and token, and the client is rebuilt automatically. The CSP allows `http://127.0.0.1:*` and `ws://127.0.0.1:*`.
- **Live status:** one WebSocket at `/ws` streams the recording status about 12 times a second. `RecordingProvider` shares it through `useRecording()` and `useLevelHistory()`, so the sidebar timer and the meters keep working across pages.
- **React lint rules:** contexts and hooks live in `lib/contexts.ts`, and providers in their own `.tsx` files, because fast refresh needs component-only files. Effects only set state inside promise or event callbacks, never synchronously.
- Shared TypeScript types live in `app/src/shared/` and are importable as `@shared/...` in the renderer. `models.ts` mirrors the backend's JSON shapes.
- **Audio format:** every track is 16 kHz mono 16-bit WAV, Whisper's native rate. Devices are opened at their own rate and channel count, then downmixed and resampled with `soxr`. A one-hour meeting is about 115 MB per track, so about 345 MB with the mix. Compressing to FLAC or Opus is a later option.
- **Track alignment:** WASAPI loopback sends nothing while nothing plays. Each audio block is stamped with the active recording time (`ActiveClock`, which excludes pauses), and `TrackWriter` feeds silence into the resampler when a track falls more than 0.25 s behind. Padding happens before resampling so the resampler's internal delay doesn't skew the count.
- **PC sound from every output:** the default "All outputs (recommended)" opens a loopback stream for every output device, writes each to `system.partN.wav` on the shared clock, and `audio_files.combine_system_parts` sums them into `system.wav` at stop. Call audio is captured even when it plays on a device other than the Windows default, such as Bluetooth earbuds switching to their hands-free device. A device that fails to open is skipped with a warning, and the meeting records which outputs were really captured. Every track is trimmed or padded to exactly the recording length.
- **Crash safety:** meetings left in `recording` or `paused` are fixed at startup by `RecordingSession.recover_interrupted()`, which repairs WAV headers, rebuilds the mix, and marks them `recovered`.
- **Storage:** SQLite at `%APPDATA%\MinuteMind\minutemind.db`, with schema migrations as a list in `store.py` tracked by `PRAGMA user_version`. Never edit a shipped migration. Add a new one. Audio lives in `%APPDATA%\MinuteMind\meetings\<id>\`. `MINUTEMIND_DATA_DIR` overrides the folder, and the tests and UI test runs use it.
- **Design system ("ink and paper"):** tokens live in `app/src/renderer/src/assets/main.css` under `@theme`, on a true black background (`ink-950` is `#000000`) with neutral grey text. Fonts are bundled through Fontsource, not loaded from Google, so the app works offline. Fraunces is for headings (`.display`), Geist for UI text, and Geist Mono for timestamps, labels (`.eyebrow`) and status. Signal red-orange is reserved for recording, marker yellow (`.marker`) for highlights, search hits and citations, and sage for healthy status. The main canvas uses `.ruled` notebook lines. `AudioPlayer` is a custom player, so transcript lines can seek it in Phase 2.

## Testing

- **Backend:** `cd backend && uv run pytest` (38 tests, one of them needs real audio devices and skips without them). A `FakeRecorder` in `tests/conftest.py` stands in for audio hardware.
- **App:** `cd app && npm run typecheck && npm run lint && npm run build`.
- **UI screenshots and scripted runs**, after `npm run build`, in `app/`:
  - Every page: `MINUTEMIND_SCREENSHOT_DIR=<folder> npx electron .`
  - Plus a real recording through the UI and a playback check: add `MINUTEMIND_SCREENSHOT_RECORD=<seconds>` and `MINUTEMIND_DATA_DIR=<scratch folder>`.
  - Quitting mid-recording instead of pressing stop: also add `MINUTEMIND_SCREENSHOT_QUIT_WHILE_RECORDING=1`.
  - Add new pages to the list in `app/src/main/devScreenshots.ts`.

## Known issues

- On this machine, `netstat` and `Get-NetTCPConnection` do not list the backend's listening socket even while it serves requests. To check the backend, read the `[minutemind] backend {...}` status line the main process logs, or call `/health` on the logged port.
- **Fixed after Phase 1 shipped:** "Failed to fetch" under the device pickers in `npm run dev` had two causes. (1) In dev the UI is served from `http://localhost:5173`, a different origin from the backend, and the backend had no CORS rules, so the browser blocked every request. `api.py` now allows `localhost` and `127.0.0.1` origins only, and the token is still required. (2) React StrictMode runs effects twice in dev, so devices were listed twice at once, and concurrent PortAudio calls crash the backend with `0xC0000005` (access violation). All PortAudio setup, listing and teardown now goes through `capture.PORTAUDIO_LOCK`, with a regression test in `tests/test_hardware.py`. **Test in dev mode too**: `npx electron-vite dev` honours the same screenshot and recording environment variables as `npx electron .`.
- When checking for backend processes, filter on `Name = 'python.exe'`. Searching command lines for "minutemind" also matches the search command itself.
