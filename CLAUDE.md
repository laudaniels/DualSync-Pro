# DualSync Pro — Development Guide

## Project Status

**Active Development:** Flask API + React web interface  
**Frozen:** Desktop app (Tkinter) as of v1.0 — see `archive/app_gui.py`

The web interface is the primary application. The desktop version is no longer maintained.

## Core Files

- **`api.py`** — Flask backend API (active)
- **`frontend/`** — React web interface (active)
- **`mashup_engine.py`** — Core audio processing engine (shared)
- **`archive/app_gui.py`** — Old Tkinter desktop app (frozen, archived)

## Running the App

```bash
python3 api.py
```

Opens at `http://localhost:5000` (requires venv with dependencies installed).

## Architecture

### `api.py`
- Flask backend serving React frontend from `frontend/dist`
- REST API endpoints for audio processing:
  - `/api/separate-stems` — stem separation
  - `/api/process-status` — real-time processing status
  - `/api/render` — render mixed audio
- CORS enabled for frontend communication

### `mashup_engine.py`
- Core audio processing engine called by API
- Handles BPM detection, stem separation, audio rendering
- FFmpeg-based mixing and effects
- Demucs for AI stem isolation
- Background thread processing for long-running tasks

### `frontend/`
- React-based web interface
- Components for mixer controls, stem management, real-time logs
- Communicates with Flask API via REST endpoints

## Development Notes

- BPM detection and stem separation run in background threads
- Presets are JSON files stored in `presets/` directory
- All audio output goes to timestamped files in project root
- Requires system FFmpeg installation

## Environment

- Python 3.10+
- Virtual environment required (checked at startup)
- See `requirements.txt` for dependencies
