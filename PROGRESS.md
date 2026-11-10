# MinuteMind progress

Read this first in every new session, then [docs/PLAN.md](docs/PLAN.md). Update it at the end of each session.

## Phase status

| Phase | Name | Status |
|---|---|---|
| 0 | Foundations | Done |
| 1 | Recording | Not started |
| 2 | Transcription after the meeting | Not started |
| 3 | Live transcript and notes | Not started |
| 4 | Speaker labels | Not started |
| 5 | Local AI and meeting minutes | Not started |
| 6 | Search and Ask | Not started |
| 7 | Claude option | Not started |
| 8 | Polish and installer | Not started |

## Next up: Phase 1, Recording

1. Add `pyaudiowpatch` and `numpy` to the backend.
2. Write `capture.py`: separate mic and WASAPI loopback streams at 16 kHz mono, written to `mic.wav` and `system.wav`, plus `mix.wav`. Handle the loopback stream going silent when nothing is playing.
3. Write `store.py`: SQLite at `%APPDATA%\MinuteMind\minutemind.db` with the `meetings` table and a simple migration version table.
4. Add API routes for devices, start, pause, stop, list meetings, and serving audio files, plus a WebSocket for level meters. WebSocket auth should use the token as a query parameter, because browsers can't set headers on WebSockets.
5. Build the Record page (device pickers, controls, meters) and the Meetings page (list and player).

## Decisions made

- The backend uses a **src layout**: code lives in `backend/src/minutemind/`, not `backend/minutemind/` as first sketched in the plan.
- **Port handshake:** the backend binds port 0 and prints `MINUTEMIND_PORT=<port>` on stdout. Electron reads that line, then polls `/health`. This avoids racing for a free port.
- **Auth:** every request needs the `X-MinuteMind-Token` header. The token is random per launch and passed through the `MINUTEMIND_TOKEN` environment variable, so it stays out of process listings.
- **No orphans:** Electron kills the process tree with `taskkill /T /F` on quit, and the backend watches Electron's PID and exits within about 2 seconds if Electron dies.
- **Dev launch:** Electron runs `backend/.venv/Scripts/python.exe` directly and falls back to `uv run` if the venv doesn't exist yet.
- **Renderer to backend:** the renderer gets `{ baseUrl, token }` from `window.api.backend.getConnection()` and calls the backend directly. The CSP allows `http://127.0.0.1:*` and `ws://127.0.0.1:*`.
- Shared TypeScript types live in `app/src/shared/` and are importable as `@shared/...` in the renderer.
- **Design system ("ink and paper"):** tokens live in `app/src/renderer/src/assets/main.css` under `@theme`. Fonts are bundled through Fontsource, not loaded from Google, so the app works offline. Fraunces is for headings (`.display`), Geist for UI text, and Geist Mono for timestamps, labels (`.eyebrow`) and status. Signal red-orange is reserved for recording, marker yellow (`.marker`) for highlights, search hits and citations, and sage for healthy status. The main canvas uses `.ruled` notebook lines.
- **UI screenshots without a browser:** after `npm run build`, run `MINUTEMIND_SCREENSHOT_DIR=<folder> npx electron .` in `app/`. The app saves a PNG of each page and quits. Add new pages to the list in `app/src/main/devScreenshots.ts`.

## Known issues

- On this machine, `netstat` and `Get-NetTCPConnection` do not list the backend's listening socket even while it serves requests. To check the backend, read the `[minutemind] backend {...}` status line the main process logs, or call `/health` on the logged port.
