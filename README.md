# MinuteMind

A desktop app that records meetings on your own PC, transcribes them, writes notes and minutes, and answers questions about past meetings with exact, cited details. Everything runs and is stored locally. An optional Claude API mode gives higher-quality answers.

- Full design: [docs/PLAN.md](docs/PLAN.md)
- Current status: [PROGRESS.md](PROGRESS.md)

## Layout

| Folder | What it is |
|---|---|
| `app/` | Electron + React + TypeScript UI (electron-vite, Tailwind) |
| `backend/` | Python 3.12 service (FastAPI, managed with `uv`) |
| `docs/` | Design and phase plan |

## Requirements

- Windows 10 or 11
- Node.js 20 or newer
- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you)

## Run in development

```bash
cd backend
uv sync

cd ../app
npm install
npm run dev
```

The Electron app starts the Python service itself on a random local port with a per-launch token. The service stops when the app closes, and also exits on its own if the app crashes.

## Tests

```bash
cd backend && uv run pytest
cd app && npm run typecheck && npm run lint
```
