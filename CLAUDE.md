# DualSync Pro — Development Guide

## Project Status

**Active Development:** Flask API + React web interface  
**Frozen:** Desktop app (Tkinter) as of v1.0 — see `archive/app_gui.py`

The web interface is the primary application. The desktop version is no longer maintained.

## Core Files

- **`server.py`** — Flask backend API (active)
- **`frontend/`** — React web interface (active)
- **`mashup_engine.py`** — Core audio processing engine (shared)
- **`archive/app_gui.py`** — Old Tkinter desktop app (frozen, archived)

## Running the App

```bash
python3 server.py
```

Opens at `http://localhost:5000` (requires venv with dependencies installed).

## Architecture

### `server.py`
- Flask backend serving React frontend from `frontend/dist`
- REST API endpoints for audio processing:
  - `/api/upload-audio` — upload a song and convert it to WAV
  - `/api/process-song` — run the chosen mode ("as is" or "align beatgrid"
    first) then analyze BPM/key and separate stems
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

## Stem Separation Modes

### Legacy Mode (7 stems) — Demucs only
Default behavior: uses Demucs v4 for 4-stem separation, then splits drums into kick/snare/hihat/tom.
- Output: `vocals`, `kick`, `snare`, `hihat`, `tom`, `bass`, `other`
- Quality: Good general-purpose
- Speed: ~5-8 min per track

### Multi-Engine Mode (13 stems) — Best-of-breed pipeline
Advanced mode combining specialized tools for maximum quality per stem-type.
Enables with: `export DUALSYNC_MULTI_ENGINE=true` before running `python3 server.py`

**Pipeline:**
1. **Stage 1 (Parallel):**
   - Mel-Band RoFormer: Lead vocals (13.67 dB SDR quality)
   - BS-RoFormer-6s: 6-stem separation (9.5 dB SDR average)
2. **Stage 2:** Demucs drum splitting (kick, snare, hihat, tom @ 9.2 dB SDR)
3. **Stage 3 (Optional):** HiFi++ GAN artifact restoration

**Output (13 stems):**
- `vocals_lead`, `vocals_backing` (from Mel-Band)
- `kick`, `snare`, `hihat`, `tom` (from Demucs)
- `bass`, `guitar`, `piano`, `strings` (from BS-RoFormer)
- `synth_lead`, `synth_pad`, `ambient` (from residual/other)

**Requirements:**
```bash
pip install audio-separator>=0.17.0
pip install julius>=0.2.8  # Optional: HiFi++ restoration
```

**Speed:** ~14-20 min per track (quality prioritized over speed)

## Development Notes

- BPM detection and stem separation run in background threads
- Multi-engine mode uses parallel GPU processing (Mel-Band + BS-RoFormer simultaneously)
- Presets are JSON files stored in `presets/` directory
- All audio output goes to timestamped files in project root
- Requires system FFmpeg installation
- Beat/BPM detection uses Essentia (`RhythmExtractor2013`); madmom was tried
  first historically but doesn't install in this project's environment
- Optional per-song "Align beatgrid" step (corrects vinyl/tempo drift via
  per-beat warping) requires the `rubberband` CLI on PATH: `apt-get install
  rubberband-cli` (Linux) or `brew install rubberband` (macOS). Without it,
  the app still works normally -- that one feature just reports a clear
  error if selected.

## Environment

- Python 3.10+
- Virtual environment required (checked at startup)
- See `requirements.txt` for dependencies
