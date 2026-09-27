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
  - `/api/upload-audio` — upload a song, convert to WAV
  - `/api/analyze-song` — fast (seconds): the chosen mode's align/snap step,
    then BPM/key analysis -- no separation yet, so the frontend can show
    detected BPM/Key (and let the user override them) before committing to
    the slow separation below
  - `/api/process-song` — separate stems from an already-analyzed WAV at its
    own detected BPM/Key ("process as is")
  - `/api/process-stems` — beatmatch/transpose the full song to a shared
    target BPM/Key, then separate -- used both for the very first
    separation (if a target was set) and to reprocess already-separated
    stems later
  - `/api/restore-stem` — apply or revert denoise + de-reverb restoration
    for one already-separated stem on demand (vocals only in the UI)
  - `/api/split-drums`, `/api/render-final-mix` — auxiliary drum-splitting
    and final-mix rendering
  - `/api/download-stems-zip`, `/api/download-unaligned-stems`,
    `/api/download-file/<filename>` — DAW-ready export packages
  - `/api/audio/<path>`, `/api/audio-stats`, `/api/process-status`,
    `/api/cleanup`, `/api/health` — serving, stats, progress polling, and
    cleanup
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

**Per-song flow** (`DualMixer.jsx`/`SongMixer.jsx`): upload → pick
as_is/align/snap (`/api/analyze-song`, seconds) → once **both** songs are
analyzed, a shared "Process as is" / "Process with Target BPM/Key" button
(label switches based on whether a target is set) kicks off separation for
both songs **in parallel** (`/api/process-song` or `/api/process-stems`) →
the volume/mixer view only appears once both songs have stems
(`revealVolumes`/`bothStemsReady`). The post-mixer BPM/Key inputs stay
available afterward to reprocess (`/api/process-stems` again, same button
group, now labeled "Process All Changes").

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
**`/api/download-stems-zip` ("📦 Download Stems" button) -- one folder per
song, named `<song>_<bpm>BPM_<key>`, with every `.wav` currently sitting in
that song's `Audio/stems/<timestamp>/` (glob-based, not a hardcoded stem
list):**
- The 9 main stems
- Bonus/reference stems (byproducts already generated for free, never
  thrown away -- see Multi-Engine Stem Separation above): the vocal
  ensemble's own two individual models' vocals + instrumental outputs
  (`extra_vocals_melband_roformer`, `extra_instrumental_melband_roformer`,
  `extra_vocals_bs_roformer`, `extra_instrumental_bs_roformer`), and
  Demucs' own unused vocals (`extra_vocals_demucs`)
- `<stem>_original.wav`/`<stem>_restored.wav` if that stem's restoration
  has been toggled (see Restoration above)
- ACID chunks embedded on every file (BPM/key for DAW auto-detect)
- No more separate "original/" vs "processed/" folders -- a song only has
  one current state at a time (whatever's currently active, as-is or
  target-processed), so there was never a real second version to split
  out; the two folders used to serve the exact same file under both labels

**`/api/download-unaligned-stems`** - pre-alignment backup (on-demand,
separate from the above): re-separates the pre-alignment WAV for whichever
song(s) used "align", since that source is never separated automatically.

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
  `/api/cleanup` clears both this and `Audio/`, but leaves the downloaded
  model weights under `~/.cache/audio-separator-models` alone (those are a
  ~3GB download, not per-song generated data)
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
