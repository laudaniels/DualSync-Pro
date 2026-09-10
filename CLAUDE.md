# DualSync Pro — Development Guide

## Project Status

**Active Development:** Gradio web interface only  
**Frozen:** Desktop app (Tkinter) as of v1.0 — see `archive/app_gui.py`

All future development focuses on the Gradio web interface (`gradio_app.py`). The desktop version is no longer maintained.

## Core Files

- **`gradio_app.py`** — Main Gradio web interface (active)
- **`mashup_engine.py`** — Core audio processing engine (shared)
- **`archive/app_gui.py`** — Old Tkinter desktop app (frozen, archived)

## Running the App

```bash
python3 gradio_app.py
```

Opens at `http://localhost:7860` (requires venv with dependencies installed).

## Architecture

### `mashup_engine.py`
- Handles BPM detection, stem separation, audio rendering
- FFmpeg-based mixing and effects
- Demucs for AI stem isolation

### `gradio_app.py`
- `StudioState` — manages all mixer state (songs, stems, BPMs, sliders, presets)
- Blocks-based Gradio UI with three main sections:
  - **Load & Analyze** — song loading, BPM detection, stem separation
  - **Per-Song Controls** — stem volumes, pitch, reverb, speed, EQ
  - **Mixing** — crossfader, target BPM, beatmatch, presets, rendering

## Development Notes

- BPM detection and stem separation run in background threads
- Presets are JSON files stored in `presets/` directory
- All audio output goes to timestamped files in project root
- Requires system FFmpeg installation

## Environment

- Python 3.10+
- Virtual environment required (checked at startup)
- See `requirements.txt` for dependencies
