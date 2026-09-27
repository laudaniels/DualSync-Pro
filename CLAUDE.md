# DualSync Pro — Development Guide

## Project Status

**Active Development:** Flask API + React web interface

The web interface is the only application. (An early Tkinter desktop version
was dropped and is not in this repository.)

## Core Files

- **`server.py`** — Flask backend API (active)
- **`frontend/`** — React web interface (active)
- **`mashup_engine.py`** — Core audio processing engine (shared)

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

**Pipeline (tested end-to-end with real audio):**
1. **Stage 1 (Parallel GPU)** — both from full song:
   - **Vocal-model ensemble** (Mel-Band RoFormer 12.6 dB SDR + BS-RoFormer
     12.1 dB SDR, averaged sample-by-sample): cleanest vocals -- verified
     by ear against each model alone, the average won clearly (a known
     technique, UVR's "Ensemble Mode": different architectures make
     different mistakes, averaging smooths those out)
   - **Demucs `htdemucs_6s`** (9.5 dB SDR): bass, guitar, piano, other, drums
   - Simultaneous processing (threading with locks)
2. **Stage 2** — from Demucs' `drums` output:
   - **MDX23C DrumSep** (SOTA): kick, snare (ML-based)
   - **Frequency-band filtering**: hihat, tom (fallback, no model)
3. **Stage 3** — assembling the 9 stems (no restoration here anymore --
   see below)

**Output (9 stems, 7 ML-separated + 2 filtered):**
- `vocals` — vocal-model ensemble (Mel-Band RoFormer + BS-RoFormer, averaged)
- `kick`, `snare` — MDX23C DrumSep (SOTA ML separation on drums stem)
- `hihat`, `tom` — Frequency-band filtering on drums stem
- `bass`, `guitar`, `piano`, `other` — Demucs `htdemucs_6s`

**Model Details:**
- **Vocal Models (ensemble, averaged):** `vocals_mel_band_roformer.ckpt` (12.60 dB SDR)
  and `model_bs_roformer_ep_368_sdr_12.9628.ckpt` ("BS-Roformer-Viperx-1296", 12.10 dB SDR)
- **Drum ML:** `drumsep_5stems_mdx23c_jarredou.ckpt` (5-stem capable, we use kick+snare)
- **Restoration (opt-in per stem, not run during separation):** `denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt`
  (27.99 dB SDR) then `dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt` (19.17 dB SDR),
  both from the audio-separator registry -- see "Restoration" below

**Requirements:**
```bash
pip install audio-separator>=0.17.0
pip install onnxruntime   # required by audio-separator
```
Both restoration models download automatically on first use, same as the
vocal/drum models above -- no separate install step.

**Past known issue (fixed):** despite this doc always naming
`vocals_mel_band_roformer.ckpt` (12.6 dB SDR) as the vocal model, the code
actually loaded `mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt`
(10.2 dB SDR) instead -- audibly worse, confirmed 2026-09-27 by a listening
test against the documented model and other candidates. Fixed in
`_separate_vocals_karaoke()` to load the documented model -- and then, in
the same session, upgraded further to the ensemble described above once an
A/B/C/D/E listening test showed averaging it with BS-RoFormer beat either
model alone. `vocals_mel_band_roformer.ckpt`'s own filename contains
"vocals", so its non-vocals "(other)" output also matched the old naive
`'vocal' in filename` check -- tightened to the parenthesized `(vocals)`
stem marker, or ensembling would have silently averaged in the wrong file
half the time.

**Past known issue (fixed):** this stage used to call the CPJKU
"music-source-restoration" project (a HiFi++ GAN) via
`restoration.mixture_inference.create_mixture_system` -- a module that never
existed in that repo, which also has no setup.py/pyproject.toml (not
pip-installable at all, just a training codebase). That import always failed
and silently fell back to spectral filtering, regardless of what was
installed. Replaced with the denoise + de-reverb models above, which are
real, tested, and use infrastructure already proven in this pipeline.

**Performance:** ~10-15 min per track (restoration no longer a mandatory
stage -- see below; vocals now run two models in sequence instead of one,
partly offsetting that saving since Stage 1 is bottlenecked by whichever
of vocals/drums finishes last)
- Stage 1 (parallel vocals ensemble + drums): ~8-11 min
- Stage 2 (drum splitting + filtering): ~2-3 min
- Stage 3 (assembling stems): seconds

## Features & Components

### Multi-Engine Stem Separation (9 stems)
**Models (best-in-class, verified SDR):**
- **Vocal-model ensemble** (Mel-Band RoFormer 12.6 dB SDR + BS-RoFormer 12.1 dB SDR, averaged) → lead vocals
- **Demucs htdemucs_6s** (9.5 dB SDR) → bass, guitar, piano, other, drums
- **MDX23C DrumSep** (SOTA) → kick, snare (from drums stem)
- **Frequency filtering** → hihat, tom (no ML model exists)

**Restoration (opt-in per stem, not part of separation):**
- **Denoise + de-reverb** (audio-separator Mel-Band Roformer models) → artifact removal
  - Denoise (27.99 dB SDR) then de-reverb (19.17 dB SDR)
  - Skipped during separation itself (was costing every song several minutes
    for all 9 stems whether or not it helped); toggle it per stem instead
    from a checkbox next to that stem's volume slider, once stems exist --
    see `/api/restore-stem`. Vocals-only in the UI: testing showed these
    models measurably hurt non-vocal stems (e.g. guitar measured ~11 dB
    quieter after "restoration") rather than helping, since they're trained
    for vocal cleanup
  - Fallback to spectral filtering if these models can't load
  - No separate install -- downloads automatically via audio-separator, same as the vocal/drum models

### UI Features
**Waveform Preview Thumbnails:**
- 120x40px waveform for each stem in mixer sliders
- Real-time HTML5 AudioContext visualization
- Color-coded (same as player) for quick data visibility
- Shows empty/quiet stems visually

**Mixer Controls:**
- Dynamic 7 or 9 volume sliders (legacy or multi-engine)
- Waveform preview per slider
- Real-time playback with stem mixing
- BPM/Key override and analysis
- Per-stem restoration toggle (vocals only -- see Restoration above),
  disabled while playing

### Download Packages
**Includes all stems:**
- `original/` - separated stems (aligned or unaligned)
- `processed/` - post-mixing versions
- ACID chunks embedded (BPM/key for DAW auto-detect)
- Multi-engine: all 9 stems included

**Beatgrid-aligned sessions:**
- `/api/download-stems-zip` - aligned + processed stems
- `/api/download-unaligned-stems` - pre-alignment backup (on-demand)

## Development Notes

- BPM detection and stem separation run in background threads
- Multi-engine mode uses parallel GPU processing (Stage 1: vocal-model ensemble + Demucs)
- Stem separation pipeline: ~10-15 min per track (quality prioritized;
  restoration is opt-in per stem afterward, not part of this)
  - Stage 1 (parallel): ~8-11 min
  - Stage 2 (drum splitting): ~2-3 min
  - Stage 3 (assembling stems): seconds
- All generated audio goes under `Audio/` (git-ignored): uploads/aligned WAVs,
  `Audio/stems/<timestamp>/`, `Audio/renders/`, and download ZIPs
- Separation results are cached in `separated_stems/<hash>/` (git-ignored);
  `/api/cleanup` only clears `Audio/`, not this cache
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
