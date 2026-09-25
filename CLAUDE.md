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
Advanced mode combining best-in-class tools for maximum quality per stem-type.
Enables with: `export DUALSYNC_MULTI_ENGINE=true` before running `python3 server.py`

Every stem is derived directly from the full song (original or beatgrid-aligned
WAV) rather than chained off another already-separated stem, with one
deliberate exception: drum-component separation needs an isolated drum stem,
not a full mix, so kick/snare runs on Demucs' `drums` output.

**Pipeline (verified end-to-end against real audio with model downloads):**
1. **Stage 1 (Parallel), both straight from the full song:**
   - **Mel-Band RoFormer** (12.6 dB SDR): cleanest lead vocals, minimal artifacts
   - **Demucs `htdemucs_6s`** (9.5 dB SDR): bass, guitar, piano, other, drums
2. **Stage 2**, from Demucs' `drums` output (intentional stem-of-stem step):
   - **MDX23C DrumSep** (SOTA): kick, snare (real ML, best-in-class)
   - **Frequency-band filtering**: hihat, tom (no ML model exists)
3. **Stage 3 (Optional):** **HiFi++ GAN** restoration (artifact removal + quality enhancement)

**Output (9 stems, 7 ML-separated + 2 filtered):**
- `vocals` — Mel-Band RoFormer (12.6 dB SDR)
- `kick`, `snare` — MDX23C DrumSep (SOTA ML separation on drums stem)
- `hihat`, `tom` — Frequency-band filtering on drums stem
- `bass`, `guitar`, `piano`, `other` — Demucs `htdemucs_6s`

**Model Details:**
- **Vocal Model:** `vocals_mel_band_roformer.ckpt` (highest quality in audio-separator registry)
- **Drum ML:** `drumsep_5stems_mdx23c_jarredou.ckpt` (5-stem capable, we use kick+snare)
- **Restoration:** CPJKU Music Source Restoration (mixture-of-experts, instrument-aware)

**Requirements:**
```bash
pip install audio-separator>=0.17.0
pip install onnxruntime   # required by audio-separator
# Optional: HiFi++ GAN for production quality
pip install git+https://github.com/CPJKU/music-source-restoration
```

**Performance:** ~14-20 min per track (quality prioritized over speed)
- Stage 1 (parallel vocals + drums): ~8-10 min
- Stage 2 (drum splitting + filtering): ~2-3 min  
- Stage 3 (HiFi++ GAN restoration): ~4-7 min (optional)

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
