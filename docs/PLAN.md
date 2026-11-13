# Plan: MinuteMind, a local meeting recorder and note taker

## Context
The user wants a desktop app on their Windows PC that records meetings, transcribes them, takes notes and writes minutes. Later, they can ask it questions and get exact answers drawn from the stored meeting data. Everything is stored locally. The empty project folder `C:\Users\nehit\Desktop\MinuteMind` already exists.

Decisions confirmed with the user:
- **AI:** both options, switchable. A built-in local model via **llama-cpp-python** (no Ollama) is the default, and the Claude API is optional.
- **Low-end hardware:** the app must still work on PCs with less than 4 GB of VRAM or no NVIDIA GPU, falling back automatically.
- **Audio:** microphone plus system audio, so remote participants in Zoom, Meet and Teams are captured.
- **Framework:** Electron with a React UI and a local Python service.
- **Speakers:** speaker labels are included in v1.

Machine facts: Windows 11, Node 24, Python 3.13, an RTX 3050 laptop GPU with about 4 GB of VRAM, and about 15 GB of RAM. Rust, Ollama and ffmpeg are not installed.

## Architecture
```
Electron (React UI)  ──HTTP + WebSocket on 127.0.0.1──►  Python service (FastAPI)
  • meeting list, live view            │                  • audio capture (mic + loopback)
  • transcript, notes, minutes          │                  • faster-whisper transcription
  • Ask panel with citations            │                  • pyannote speaker labels
  • settings, API key (safeStorage)     │                  • SQLite + FTS5 + embeddings
                                        │                  • LLM provider: llama.cpp | Claude
                                        │                  • hardware profile + model manager
                                        ▼
                       %APPDATA%\MinuteMind\  (db + per-meeting audio)
```
- When Electron's main process starts, it launches the Python service on a random free port with a random session token. It then waits for `/health` and stops the service when the app quits.
- Live transcript text and recording levels stream to the UI over a WebSocket.

## Tech choices
| Concern | Choice | Why |
|---|---|---|
| UI | electron-vite, React, TypeScript, Tailwind | Same stack as the user's other projects |
| Python runtime | `uv` venv pinned to **Python 3.12** | torch, pyannote and PyAudioWPatch have reliable 3.12 wheels. 3.13 support is patchy. |
| Capture | **PyAudioWPatch** (WASAPI loopback and mic) | Captures system audio on Windows without a virtual cable |
| Transcription | **faster-whisper** on CUDA: `small` int8 for the live view, `large-v3-turbo` int8_float16 for the final pass | Fits in 4 GB of VRAM. Falls back to CPU automatically. |
| Speakers | Separate mic and system tracks: the mic track is labelled "You", and **pyannote/speaker-diarization-3.1** labels the system track | You get labelled for free, and other people get labels such as Speaker 1 and Speaker 2. Speakers can be renamed in the UI. |
| Storage | SQLite with an **FTS5** index, plus embeddings stored as BLOBs | One file, exact keyword search, and no extra server |
| Embeddings | `BAAI/bge-small-en-v1.5` via sentence-transformers, run on the CPU | Small and good enough for meeting-scale data |
| Local LLM | **llama-cpp-python** running Qwen 2.5 3B Instruct GGUF (Q4_K_M), loaded in-process. Install the prebuilt CUDA 12 wheel and fall back to the CPU wheel. | No separate install. The app controls loading and unloading, so the GPU can be handed between Whisper, pyannote and the LLM. `n_gpu_layers` splits the model across GPU and RAM on small GPUs. JSON-schema output keeps minutes well-formed. |
| Model files | `huggingface_hub` downloads into `%APPDATA%\MinuteMind\models\` on first run, with a progress bar | One place for Whisper, pyannote, embedding and LLM files |
| Cloud LLM | Anthropic Python SDK, **`claude-opus-5-5`**, streaming, adaptive thinking, and server-side refusal fallbacks (`fallbacks: "default"`) enabled by default | Off until the user enters a key. The UI shows a "Cloud" badge whenever it is in use. |

## Project layout
```
MinuteMind/
  app/                      Electron + React (electron-vite)
    src/main/               window, Python process manager, safeStorage key store
    src/preload/            typed IPC bridge
    src/renderer/           pages: Meetings, Recording, Meeting detail, Ask, Settings
  backend/                  Python service (uv project)
    src/minutemind/
      api.py                FastAPI routes + WebSocket
      capture.py            mic + loopback recorder → mic.wav, system.wav, mix.wav
      transcribe.py         faster-whisper live + final passes
      diarize.py            pyannote on system track, merge with word timestamps
      store.py              SQLite schema, FTS5, migrations
      index.py              chunking + embeddings
      qa.py                 hybrid retrieval + answer with citations
      minutes.py            summary / decisions / action items (structured JSON)
      hardware.py           detect GPU/VRAM/RAM → choose profile
      models.py             download, load, unload; one GPU user at a time
      llm/                  base.py, llamacpp.py, claude.py
    tests/
  README.md
```

## Data model (SQLite)
- `meetings`: id, title, started_at, ended_at, audio_dir, status.
- `segments`: meeting_id, start_ms, end_ms, speaker, text. An FTS5 table mirrors the text column.
- `speakers`: meeting_id, label, display_name.
- `notes`: meeting_id, ts_ms, text. These are the user's own notes typed during the meeting and are timestamped.
- `minutes`: meeting_id, summary, decisions JSON, action_items JSON (task, owner, due), topics JSON, model_used.
- `chunks`: meeting_id, start_ms, end_ms, text, embedding BLOB. Each chunk covers about 45 seconds of speech, with overlap.

## Hardware profiles
At startup, `hardware.py` reads free VRAM through CTranslate2/torch CUDA queries and checks total RAM. It then picks a profile, which the user can override in Settings.

| Profile | When | Live transcript | Final transcript | Speakers | Local LLM |
|---|---|---|---|---|---|
| GPU | NVIDIA with 4 GB or more | `small` on GPU | `large-v3-turbo` on GPU | GPU | 3B, all layers on GPU |
| Small GPU | NVIDIA with 2 to 4 GB | `small` on GPU | `large-v3-turbo` on GPU | GPU | 3B, split between GPU and RAM |
| CPU | No NVIDIA GPU, or under 2 GB | `base` on CPU | `large-v3-turbo` on CPU, in the background | CPU | 3B on CPU, with Qwen 2.5 1.5B as an option |

- Models run one at a time. `models.py` unloads one before loading the next and frees CUDA memory in between.
- If the GPU runs out of memory, that step is retried on the CPU instead of failing.
- The UI shows the active profile and an estimate of processing time after each meeting.
- With the Claude provider selected, the local LLM is never loaded.

## How "exact details" Q&A works
1. Search runs two ways and the results are merged with reciprocal-rank fusion. FTS5 BM25 finds exact words, names and numbers. Embedding similarity finds passages with the same meaning.
2. The top chunks, with their speaker and timestamp, go to the selected LLM. The prompt requires the model to quote the source, cite `[Meeting, mm:ss]`, and reply "not found in the recordings" when the answer isn't in the chunks.
3. Clicking a citation in the UI opens that meeting and plays the audio from that moment.
4. Questions can cover one meeting or all meetings.

## Phase plan
The app is built in nine phases, one or two working sessions each. Every phase ends with the app running, tests passing, and a git commit tagged `phase-N`. Later phases build on earlier ones, but no phase leaves the app broken.

**Carrying work between sessions:** the repo keeps a `PROGRESS.md` file listing each phase's status, decisions made, and known issues. Each session starts by reading it and ends by updating it, so a fresh session can continue without re-explaining anything.

### Phase 0: Foundations
- **Goal:** an empty app shell that starts and stops its Python service correctly.
- **Build:** install `uv`. Run `git init` with a `.gitignore`, add `README.md` and `PROGRESS.md`, scaffold electron-vite with React, TypeScript and Tailwind, and create a uv backend pinned to Python 3.12 with FastAPI. Add the process manager in Electron main with a random port and session token, a `/health` route, and a "Backend connected" status in the UI.
- **Done when:** `npm run dev` opens the window showing "Backend connected", and closing it leaves no Python process running.

### Phase 1: Recording
- **Goal:** record a meeting and play it back.
- **Build:** `capture.py` records mic and WASAPI loopback to `mic.wav` and `system.wav`, plus a mixed track. Add device pickers, start, pause and stop controls, and live level meters over WebSocket. `store.py` creates the SQLite schema with a `meetings` table. Add a meetings list and an audio player.
- **Done when:** a 30-second test recording, with a YouTube clip playing while you speak, saves both tracks with audio and appears in the list with working playback.

### Phase 2: Transcription after the meeting
- **Goal:** get an accurate, timestamped transcript of every recording.
- **Build:** `hardware.py` picks a profile, and `models.py` handles first-run downloads with a progress bar plus loading and unloading. `transcribe.py` runs the `large-v3-turbo` pass as a background job with progress. Add the `segments` table, and a transcript view where clicking a line jumps the audio to that point. If the GPU runs out of memory, the step retries on the CPU.
- **Done when:** the test recording's transcript is correct, and the forced CPU profile also completes.

### Phase 3: Live transcript and notes
- **Goal:** see words appear while the meeting is happening, and type notes alongside.
- **Build:** run a rolling 10-second transcription with the small model during recording and stream it to the UI. Add a timestamped notes editor saved to the `notes` table. The final pass from Phase 2 replaces the live text afterward.
- **Done when:** live text appears within about 15 seconds of speech, and notes show next to the matching moment in the transcript.

### Phase 4: Speaker labels
- **Goal:** know who said what.
- **Build:** add a Hugging Face token field in Settings. `diarize.py` runs pyannote on the system track and merges its speaker turns with word timestamps. Mic speech is labelled "You". Add the `speakers` table and renaming in the UI.
- **Done when:** a test call with two remote voices shows "You", Speaker 1 and Speaker 2 correctly, and renaming a speaker updates the whole transcript.

### Phase 5: Local AI and meeting minutes
- **Goal:** automatic minutes with no internet needed.
- **Build:** install llama-cpp-python, trying the CUDA wheel first with the CPU wheel as a fallback. Add the `llm/` provider interface with `llamacpp.py`, with GPU hand-off through `models.py`. `minutes.py` produces the summary, decisions, action items and topics as schema-checked JSON. Add a minutes view and Markdown export.
- **Done when:** minutes for the test meeting list the real decisions and action items, and `nvidia-smi` shows only one model using the GPU at a time.

### Phase 6: Search and Ask
- **Goal:** ask questions and get exact answers with sources.
- **Build:** `index.py` handles chunking, bge-small embeddings and the FTS5 index. `qa.py` runs hybrid retrieval with reciprocal-rank fusion and an answer prompt that requires citations. Add an Ask panel with streamed answers, a choice of one meeting or all meetings, and citations that open the meeting at that moment.
- **Done when:** asking for a specific number or name from the test meeting returns it with a working citation, and an unrelated question returns "not found".

### Phase 7: Claude option
- **Goal:** an optional, higher-quality cloud AI.
- **Build:** store the API key in Electron safeStorage. `llm/claude.py` uses `claude-opus-5-5` with streaming, adaptive thinking and server-side refusal fallbacks. Add a provider switch in Settings and a "Cloud" badge whenever Claude is used.
- **Done when:** the same question works with both providers, and the Cloud badge appears only for Claude.

### Phase 8: Polish and installer
- **Goal:** something you can install and hand to someone else.
- **Build:** add a first-run setup wizard covering hardware check, model downloads and an optional token and key, plus the recording-consent banner. Add friendly error messages, then package with PyInstaller and an electron-builder NSIS installer.
- **Done when:** the installer works on a clean Windows user account, and a full record, transcribe, minutes and ask cycle completes in both the GPU and CPU profiles.

### Where you have something usable
- **After Phase 2:** a recorder with accurate transcripts.
- **After Phase 5:** automatic minutes, fully offline.
- **After Phase 6:** the full core idea, including asking questions about past meetings.

## One-time setup the user will need
- Install `uv`. No Ollama is needed.
- No Hugging Face token is needed: speaker labels use sherpa-onnx models from GitHub (see PROGRESS.md).
- Optionally, add an Anthropic API key in Settings to use Claude.
- The first run downloads the Whisper, embedding, pyannote and LLM models, about 4 GB in total.
- If the prebuilt CUDA wheel for llama-cpp-python doesn't match, building it needs Visual Studio Build Tools with C++. The CPU wheel always works as a fallback.

## Risks and notes
- **VRAM:** Whisper, pyannote and the LLM run one at a time and unload in between. Running out of GPU memory falls back to the CPU.
- **CPU-only speed:** a one-hour meeting can take about 30 to 60 minutes to fully transcribe on the CPU. This runs in the background, and the live transcript stays usable meanwhile.
- **Consent:** a banner reminds the user to tell participants the meeting is being recorded, which is legally required in many places.
- **Privacy:** audio never leaves the PC. In Claude mode, only the retrieved transcript excerpts are sent.

## Verification
- **Backend:** `uv run pytest` covers the store, chunking, retrieval ranking and minutes JSON validation, using a short sample WAV with known words.
- **Recording:** play a YouTube clip while speaking into the mic, record for 30 seconds, then confirm both tracks contain audio and the transcript labels "You" separately from the other speaker.
- **Q&A:** ask for a specific number or name said in the test recording. Confirm the exact answer comes back with a working timestamp citation, and that an unrelated question returns "not found".
- **Providers:** run the same question with the local model and with Claude, and confirm the Cloud badge appears only for Claude.
- **Low-end fallback:** force the CPU profile in Settings, or set `CUDA_VISIBLE_DEVICES=-1`, and confirm recording, transcription, minutes and Q&A all still complete.
- **GPU hand-off:** watch `nvidia-smi` during a post-meeting run and confirm only one model holds VRAM at a time, with usage staying under 4 GB.
- **App:** run `npm run dev` in `app/` and confirm it launches the backend, and that closing the window leaves no orphan Python process.
