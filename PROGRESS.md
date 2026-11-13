# MinuteMind progress

Read this first in every new session, then [docs/PLAN.md](docs/PLAN.md). Update it at the end of each session.

## Phase status

| Phase | Name | Status |
|---|---|---|
| 0 | Foundations | Done |
| 1 | Recording | Done (see the open check below) |
| 2 | Transcription after the meeting | Done |
| 3 | Live transcript and notes | Done |
| 4 | Speaker labels | Done |
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

### Phase 2 results

Checked with a 20-second test meeting made from two text-to-speech clips ("You" at 0.5 s, "Others" at 9.5 s), run through the real app:

- `large-v3-turbo` on the RTX 3050 transcribed both tracks word for word ("the 14th of March", "$420 per month", "Priya"), with the right speaker and start times. GPU memory peaked at about 1.9 GB of 4 GB.
- Processor only gave the same text in 38 s, including loading the model, so a long meeting on the CPU takes roughly twice its length.
- Clicking a transcript line jumps the player there and highlights the line.

### Phase 3 results

Checked in the real app, with speech clips playing into the inaudible Voice Changer device and captured as "Others":

- The live transcript showed lines about 10 s after speech (6 to 10 lines in 25 to 30 s), labeled You and Others.
- A note typed 5 s into the recording was saved at 0:04 and appears at that point in the finished large-v3-turbo transcript.
- Quiet or noisy microphone audio once made the live model switch to Urdu script. Live mode now locks onto the first confident language and drops lines Whisper rates as probably not speech.

Live tests were run with `MINUTEMIND_LIVE_MODEL=tiny`, because the connection was too slow to fetch `small` in time. `small` finished downloading afterwards (464 MB in `%APPDATA%\MinuteMind\models\small`, after resuming from a stalled transfer). On the RTX 3050 it transcribed the test clip correctly in 0.46 s per 9 s slice once loaded (about 5 s to load). A short real recording is still worth doing to judge live quality on real speech.

**Privacy note for testing:** scripted recordings capture the real default microphone. Delete scratch data folders after testing, and unlink any `models` junctions first with `rmdir`, never `rm -rf`.

### Phase 4 results

Checked with a 40-second test call: three Windows text-to-speech voices on the PC-sound track (Zira, David, Hazel, David, Zira) plus a "You" line on the mic, run through real large-v3-turbo and real speaker detection:

- All five turns went to the right person (Speaker 1 = Zira twice, Speaker 2 = David twice, Speaker 3 = Hazel), and the mic lines stayed "You". Transcription and speaker detection took 17 s.
- Renaming Speaker 1 to "Priya" in the UI updated the chip and every one of her lines.
- Re-detecting with a count works: told 2 people it merges the two female voices, told 3 it matches the truth.

## Next up: Phase 5, Local AI and meeting minutes

1. Install `llama-cpp-python`. Try the prebuilt CUDA 12 wheel first (`--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124` or newer; check which index supports Python 3.12 on Windows) and fall back to the CPU wheel. Building from source needs Visual Studio Build Tools, so avoid it. The connection has been slow at times (sometimes about 70 KB/s from Hugging Face), so check download speed first. The GitHub release download used for the speaker models was fast.
2. Model: Qwen 2.5 3B Instruct GGUF Q4_K_M (about 2 GB) downloaded into `%APPDATA%\MinuteMind\models\`. Reuse the retrying download pattern. Consider `huggingface_hub.hf_hub_download` with `local_dir`, plus progress by watching bytes, as in `models.download_whisper`.
3. `llm/` provider interface (`base.py`, `llamacpp.py`), loaded through `models.ModelManager` so it never shares the GPU with Whisper. Choose `n_gpu_layers` from the hardware profile: all layers on gpu, partial on small_gpu, 0 on cpu.
4. `minutes.py`: summary, decisions, action items (task, owner, due) and topics as JSON constrained by a schema (llama.cpp `response_format` json_schema / grammar). Long meetings need chunking: summarize sections, then combine. Feed speaker names (from `list_speakers`) and notes into the prompt.
5. Migration 5: a `minutes` table. A queue job kind "minutes" runs after transcription when a setting is on, plus `POST /meetings/{id}/minutes` to regenerate. A minutes view in the meeting detail, and Markdown export (save dialog through Electron IPC).
6. Done when: minutes for the test meeting list its real decisions and action items, and `nvidia-smi` shows only one model using the GPU at a time.

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
- **Transcription (Phase 2):** `transcribe.TranscriptionQueue` runs one meeting at a time on a worker thread. It transcribes `mic.wav` and `system.wav` separately, so each line is already "you" or "others", and drops mic lines that are near-duplicates of an overlapping PC-sound line (speaker bleed when using laptop speakers). Silent tracks are skipped, and unfinished jobs resume on startup. When a recording stops, `RecordingSession.on_finished` queues it if the `auto_transcribe` setting is on. Recovered meetings are queued too.
- **Speech model:** faster-whisper `large-v3-turbo` (int8_float16 on the GPU, int8 on the CPU), with `vad_filter`, word timestamps and `condition_on_previous_text=False`. Audio goes in as a float32 numpy array read from our WAVs. faster-whisper's own decoder breaks with the installed PyAV (`metadata_errors` error), so never pass it a file path. The model downloads once to `%APPDATA%\MinuteMind\models\large-v3-turbo` (about 1.5 GB). `models.download_whisper` reports progress by watching bytes on disk, and `models.ModelManager` keeps only one model loaded. The queue unloads it whenever it goes idle, to free GPU memory.
- **GPU on Windows:** CUDA libraries come from pip (`nvidia-cublas-cu12` pinned below 12.9, because 12.9 has no Windows wheel, and `nvidia-cudnn-cu12` 9.x). `gpu_env.prepare()` adds their `bin` folders to the DLL search path and must run before importing faster_whisper or ctranslate2. If the GPU fails (out of memory, CUDA errors), the job retries on the CPU.
- **Hardware profile:** `hardware.detect()` uses `nvidia-smi`, CTranslate2's CUDA device count and `psutil`. `choose_profile()` maps it to gpu (3.8 GB or more of video memory), small_gpu (2 GB or more) or cpu, honouring the `processing` setting (auto, gpu, cpu). Detection is cached per backend run (`HardwareCache`).
- **Settings:** a key/value `settings` table (migration 2) with defaults in `store.DEFAULT_SETTINGS`, exposed at `GET/PATCH /settings`.
- **Transcript API:** `POST /meetings/{id}/transcribe` (202), `GET /meetings/{id}/transcript` (segments without words, for size; words are stored for later phases), `GET /system/hardware`. `/ws` now sends a `transcription` message alongside `recording`, and the UI reads it through `useTranscriptionJobs()`.
- **Live transcript (Phase 3):** `capture.Recorder` takes an `AudioTap`, and each `TrackWriter` passes every 16 kHz chunk with its timeline position to `live.LiveTranscriber.feed`. A `LiveBuffer` per speaker sums overlapping output devices and forgets audio once transcribed. The worker transcribes about 10 s slices (`CHUNK_S`) cut at the quietest 0.2 s in the last 3 s, so words aren't split, and skips silent slices. It uses `small` on the GPU and `base` on the CPU (`live_model_for`, overridable with `MINUTEMIND_LIVE_MODEL`), with beam size 1 and the language lock above. Lines are append-only with stable ids, and `/ws` sends a `live` message with only new lines (`reset` when a new recording starts). The client sorts by `start_ms`. Live lines are never stored. The final pass replaces them.
- **GPU sharing while recording:** `RecordingSession.on_started` pauses the `TranscriptionQueue`. A running job finishes, but new jobs wait. The live worker waits for `gpu_free()` (no job running) before loading its model. On stop, the session unloads the live model (`live.stop()`) before `on_finished` queues the final pass and `on_stopped` resumes the queue. The `live_transcript` setting turns it off.
- **Notes:** migration 3 adds a `notes` table (`ts_ms`, `text`, cascade on meeting delete). `POST /meetings/{id}/notes` stamps the current recording time when `ts_ms` is omitted during a recording. After a meeting, `ts_ms` is required, and the UI sends the player position. There are also `PATCH` and `DELETE /notes/{id}`. The transcript view merges notes with spoken lines by time.
- **Model downloads retry:** `models.download_whisper` retries up to 6 times with backoff, resuming partial files. A download that hangs without erroring can still stall. If that happens, restarting the process resumes it.
- **Speaker labels (Phase 4), changed from the plan:** the plan said pyannote plus a Hugging Face token. pyannote needs PyTorch (2-3 GB) and the user would have to accept gated model terms. We use **sherpa-onnx** instead (`diarize.py`): the same pyannote segmentation-3.0 model exported to ONNX, plus a WeSpeaker ResNet34 (VoxCeleb, English) voice-fingerprint model, run on the processor through ONNX Runtime. No token, about 33 MB of models downloaded once from GitHub releases into `models/diarization/`, with a retrying, resuming urllib download. 8 threads take about 10% of real time.
- **How speakers are assigned:** only `system.wav` is diarized, since the mic is "you". `assign_speakers` gives each word to the speaker overlapping it most (or the nearest, for words in gaps) and splits a line where the speaker changes. Lines with one speaker keep their original text and timing exactly. Speakers are labeled S1, S2... by first appearance. `CLUSTER_THRESHOLD = 0.3`: 0.4 merged two similar female voices and 0.2 over-split. Real voices may need tuning, and the UI offers "detect again" with a people count and merging by giving two speakers the same name.
- **Speaker storage and jobs:** migration 4 adds `segments.speaker`, `meetings.speakers_status/speakers_error` and a `speakers` table (label, display_name). Detection is a stage of the transcription job (after Whisper, which is unloaded first) and never fails the transcript. If it fails, lines stay "Others". `TranscriptionQueue` jobs now have a kind: "full", or "speakers" (re-detect only, optional `num_speakers`). Routes: `PATCH /meetings/{id}/speakers/{label}` and `POST /meetings/{id}/speakers/detect`. The `speaker_labels` setting turns it off. Re-detecting resets names, because the numbering can change.
- **Crash safety:** meetings left in `recording` or `paused` are fixed at startup by `RecordingSession.recover_interrupted()`, which repairs WAV headers, rebuilds the mix, and marks them `recovered`.
- **Storage:** SQLite at `%APPDATA%\MinuteMind\minutemind.db`, with schema migrations as a list in `store.py` tracked by `PRAGMA user_version`. Never edit a shipped migration. Add a new one. Audio lives in `%APPDATA%\MinuteMind\meetings\<id>\`. `MINUTEMIND_DATA_DIR` overrides the folder, and the tests and UI test runs use it.
- **Design system (black and grey):** tokens live in `app/src/renderer/src/assets/main.css` under `@theme`, on a true black background (`ink-950` is `#000000`) with neutral grey text. Fonts are bundled through Fontsource, not loaded from Google, so the app works offline. Fraunces is for headings (`.display`), Geist for UI text, and Geist Mono for timestamps, labels (`.eyebrow`) and status. Colour only where it means something: muted red (`signal`) for the record dot and errors, and muted green (`ok`) for a healthy engine. Everything else, including the active page, selected tabs, the player progress and focus outlines, is white or grey. The user asked for no neon or generic AI styling, so do not add glows, pulses, gradients or highlighter effects. The main canvas is plain black, with no background lines or patterns. `AudioPlayer` is a custom player, so transcript lines can seek it in Phase 2.

## Testing

- **Backend:** `cd backend && uv run pytest` (89 tests, one of them needs real audio devices and skips without them). `tests/conftest.py` has a `FakeRecorder` for audio hardware, a `FakeEngine` for Whisper (it can simulate the GPU running out of memory) and a `FakeHardwareCache`. The API tests always inject these, so no test downloads a real model.
- **App:** `cd app && npm run typecheck && npm run lint && npm run build`.
- **UI screenshots and scripted runs**, after `npm run build`, in `app/`:
  - Every page: `MINUTEMIND_SCREENSHOT_DIR=<folder> npx electron .`
  - Plus a real recording through the UI and a playback check: add `MINUTEMIND_SCREENSHOT_RECORD=<seconds>` and `MINUTEMIND_DATA_DIR=<scratch folder>`.
  - Quitting mid-recording instead of pressing stop: also add `MINUTEMIND_SCREENSHOT_QUIT_WHILE_RECORDING=1`.
  - Open the newest meeting, wait for its transcript and click a line: `MINUTEMIND_SCREENSHOT_TRANSCRIPT=<max seconds>`. To test transcription without downloading the model again, link the scratch data folder's `models` to the real one: `mklink /J <scratch>\models %APPDATA%\MinuteMind\models`.
  - Speech samples for tests can be made with Windows text-to-speech (`System.Speech.Synthesis.SpeechSynthesizer`, 16 kHz mono 16-bit). Installed voices: Microsoft David (male, US), Zira (female, US) and Hazel (female, UK), which is enough for multi-speaker tests. The transcript flow in the screenshot mode also renames the first speaker to "Priya".
  - To get speech into a recording without making noise, play WAVs into the "Voice Changer Virtual Audio Device" output with pyaudiowpatch. "All outputs" captures it as Others. With `MINUTEMIND_SCREENSHOT_RECORD`, the scripted recording also types a note 5 s in and logs how many live lines appeared.
  - Add new pages to the list in `app/src/main/devScreenshots.ts`.

## Known issues

- On this machine, `netstat` and `Get-NetTCPConnection` do not list the backend's listening socket even while it serves requests. To check the backend, read the `[minutemind] backend {...}` status line the main process logs, or call `/health` on the logged port.
- **Fixed after Phase 1 shipped:** "Failed to fetch" under the device pickers in `npm run dev` had two causes. (1) In dev the UI is served from `http://localhost:5173`, a different origin from the backend, and the backend had no CORS rules, so the browser blocked every request. `api.py` now allows `localhost` and `127.0.0.1` origins only, and the token is still required. (2) React StrictMode runs effects twice in dev, so devices were listed twice at once, and concurrent PortAudio calls crash the backend with `0xC0000005` (access violation). All PortAudio setup, listing and teardown now goes through `capture.PORTAUDIO_LOCK`, with a regression test in `tests/test_hardware.py`. **Test in dev mode too**: `npx electron-vite dev` honours the same screenshot and recording environment variables as `npx electron .`.
- When checking for backend processes, filter on `Name = 'python.exe'`. Searching command lines for "minutemind" also matches the search command itself.
