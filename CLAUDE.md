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

### Multi-Engine Mode (9 stems) — Best-of-breed pipeline
Advanced mode combining specialized tools for maximum quality per stem-type.
Enables with: `export DUALSYNC_MULTI_ENGINE=true` before running `python3 server.py`

Every stem is derived directly from the full song (original or beatgrid-aligned
WAV) rather than chained off another already-separated stem, with one
deliberate exception: drum-component separation needs an isolated drum stem,
not a full mix, so it runs on Demucs' `drums` output instead of the song
itself.

**Pipeline (verified end-to-end against a real audio clip, incl. actual model
downloads — see notes below for what that testing changed vs. the original
design):**
1. **Stage 1 (Parallel), both straight from the full song:**
   - Mel-Band Roformer Karaoke: clean lead vocals
   - Demucs `htdemucs_6s`: bass, guitar, piano, other, drums
2. **Stage 2**, both from Demucs' `drums` output (the one intentional
   stem-of-stem step — see note above):
   - MDX23C DrumSep (ML): kick, snare
   - Frequency-band filtering (approximate, not ML): hihat, tom
3. **Stage 3 (Optional):** HiFi++ GAN artifact restoration

**Output (9 stems):**
- `vocals` (Mel-Band Roformer Karaoke)
- `kick`, `snare` (MDX23C DrumSep, from Demucs' drums — real ML separation)
- `hihat`, `tom` (bandpass-filtered from Demucs' drums — approximate, not ML)
- `bass`, `guitar`, `piano`, `other` (Demucs `htdemucs_6s`)

Two things changed after testing against real audio (not guessed):
- The "Karaoke" model's second output is a generic instrumental (full mix
  minus vocals), not isolated backing vocals, and no dedicated backing-vocal
  model exists in the `audio-separator` registry — so there's a single
  `vocals` stem, not `vocals_lead`/`vocals_backing`.
- MDX23C DrumSep only separates kick + snare, not hihat/tom — there's no ML
  model for those, so they fall back to frequency-band filtering (same
  technique the legacy 7-stem mode already uses).

There is also no dedicated model for strings/synth/ambient separation, so
those categories were dropped rather than faked as duplicates of `other`.

**Requirements:**
```bash
pip install audio-separator>=0.17.0
pip install onnxruntime   # required by audio-separator
pip install julius>=0.2.8  # Optional: HiFi++ restoration
```

**Speed:** ~14-20 min per track (quality prioritized over speed)

## Development Notes

- BPM detection and stem separation run in background threads
- Multi-engine mode uses parallel processing (Karaoke vocal model + Demucs `htdemucs_6s` simultaneously)
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
