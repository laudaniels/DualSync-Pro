# DualSync Pro — Development Guide

## Project Status

**Active Development:** Flask API + React web interface

The web interface is the only application. (An early Tkinter desktop version
was dropped and is not in this repository.)

## Core Files

- **`server.py`** — Flask backend API (active)
- **`frontend/`** — React web interface (active)
- **`mashup_engine.py`** — Core audio processing engine (shared)
- **`validate_pipeline.py`** — standalone dev/QA script: runs the real kick-
  transient detection + beatgrid logic (`_detect_kick_candidates`,
  `_detect_beats_essentia`) against a folder of real audio files and prints
  a summary (candidates found, chosen trim point, processing time, crashes)
  -- for sanity-checking the constants in "Beatgrid & Kick-Transient
  Detection" below against real, diverse music instead of only synthetic
  test fixtures. Usage: `python3 validate_pipeline.py <folder>`. Put test
  tracks in `Audio/validation_tracks/` (already git-ignored).

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
    cleanup (`/api/health` also returns the environment/dependency checks
    from `MashupEngine.check_environment()` -- see "Startup: Dependency
    Checks & Model Prefetch" below)
  - `/api/startup-status` — polled by the frontend's blocking startup
    overlay while dependency checks and model downloads run on their own
    background thread (see "Startup: Dependency Checks & Model Prefetch")
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
Opt-out fallback: uses Demucs v4 for 4-stem separation, then splits drums into kick/snare/hihat/tom.
Enable with: `export DUALSYNC_MULTI_ENGINE=false` before running `python3 server.py`
- Output: `vocals`, `kick`, `snare`, `hihat`, `tom`, `bass`, `other`
- Quality: Good general-purpose
- Speed: ~5-8 min per track

### Multi-Engine Mode (9 stems) — Best-of-breed pipeline
**Default mode** -- combines best-in-class tools for maximum quality per
stem-type; no environment variable needed to get this, it's what
`python3 server.py` gives you out of the box (`os.getenv('DUALSYNC_MULTI_ENGINE', 'true')` in
`server.py`).

Every stem is derived directly from the full song (original or beatgrid-aligned
WAV) rather than chained off another already-separated stem, with one
deliberate exception: drum-component separation needs an isolated drum stem,
not a full mix, so kick/snare runs on the (refined, see below) `drums` output.

**Pipeline (tested end-to-end with real audio):**
1. **Stage 1 (Parallel GPU)** — both from full song:
   - **Vocal-model ensemble** (Mel-Band RoFormer by becruily + BS-RoFormer
     12.1 dB SDR, averaged sample-by-sample): cleanest vocals -- verified by
     ear in a live A/B/C test against a real track, the ensemble beat
     becruily alone by a small margin and both beat the old ensemble outright
     (a known technique, UVR's "Ensemble Mode": different architectures make
     different mistakes, averaging smooths those out)
   - **Demucs `htdemucs_6s`** (9.5 dB SDR): guitar, piano, other -- plus an
     initial bass/drums pass, refined by the next bullet (it's the only
     model that separates guitar/piano at all, but trades away bass/drums
     quality for that; see `_separate_bass_drums_mmi`'s docstring)
   - **Demucs `hdemucs_mmi`**: re-extracts just bass (12.2 dB SDR) and
     drums (9.6 dB SDR) from the same full song, sequentially after
     htdemucs_6s within the same thread -- both feed into Stage 2/3 in
     place of htdemucs_6s's own (worse) bass/drums
   - Vocal ensemble and the two Demucs passes run in parallel with each
     other (threading with locks); the two Demucs passes run sequentially
     with each other (GPU memory headroom, not a correctness requirement)
2. **Stage 2** — from the refined `drums` output:
   - **MDX23C DrumSep** (SOTA): kick, snare (ML-based)
   - **Frequency-band filtering**: hihat, tom (fallback, no model)
3. **Stage 3** — assembling the 9 stems (no restoration here anymore --
   see below)

**Output (9 stems, 7 ML-separated + 2 filtered):**
- `vocals` — vocal-model ensemble (Mel-Band RoFormer + BS-RoFormer, averaged)
- `kick`, `snare` — MDX23C DrumSep (SOTA ML separation on the refined drums stem)
- `hihat`, `tom` — Frequency-band filtering on the refined drums stem
- `bass`, `drums` (source stem) — Demucs `hdemucs_mmi`
- `guitar`, `piano`, `other` — Demucs `htdemucs_6s`

**Model Details:**
- **Vocal Models (ensemble, averaged):** `mel_band_roformer_vocals_becruily.ckpt` (no SDR
  listed in audio-separator's own registry; chosen by live listening test, not a benchmark
  number) and `model_bs_roformer_ep_368_sdr_12.9628.ckpt` ("BS-Roformer-Viperx-1296", 12.10 dB SDR)
- **Drum ML:** `drumsep_5stems_mdx23c_jarredou.ckpt` (5-stem capable, we use kick+snare)
- **Restoration (opt-in per stem, not run during separation):** `denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt`
  (27.99 dB SDR) then `dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt` (19.17 dB SDR),
  both from the audio-separator registry -- see "Restoration" below

**Past known issue (fixed):** `htdemucs_6s` was used for bass and drums too
(not just guitar/piano/other), but it's the *worst*-scoring Demucs variant
for both (bass 10.10 dB, drums 8.47 dB SDR) -- the price of the extra
guitar/piano split it alone provides. Since kick/snare/hihat/tom all derive
from the drums stem, this was quietly degrading four stems, not just one.
Confirmed 2026-09-30 by ear (bass sounded bad enough to investigate) and by
registry SDR across every Demucs variant. Fixed by adding a second Demucs
pass (`hdemucs_mmi`, bass 12.23 dB / drums 9.64 dB) whose bass/drums
override htdemucs_6s's own in `extract_instruments()` -- htdemucs_6s is
kept only for guitar/piano/other, since it's the only model in the
registry that separates those at all. (A dedicated MVSEP ensemble bass
model scores higher still, ~13.3 dB, but isn't in the audio-separator
registry and would need a separate, heavier integration -- not pursued.)

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

**Past decision (superseded, 2026-10-01):** `vocals_mel_band_roformer.ckpt`
was replaced in the ensemble by `mel_band_roformer_vocals_becruily.ckpt`
(also in audio-separator's registry, no SDR listed there). Checked
audio-separator's model registry directly for anything better than the
existing pipeline across every stem category; for vocals this surfaced
becruily's model as a real, already-downloadable candidate. A live A/B/C
listening test against a real track (not a benchmark number) confirmed it:
becruily alone beat the old ensemble outright, and ensembling it with
BS-Roformer (unchanged) beat becruily alone by a smaller margin -- so only
the Mel-Band half of the ensemble changed. The same registry check found no
comparably-available upgrade for bass/drums/guitar/piano/other (nothing
clearly better that's already in audio-separator or a plain Demucs preset),
and confirmed (by actually running it) that the kick/snare model already in
use genuinely only outputs kick+snare, not the hihat/tom/ride/crash its own
training config lists -- so hihat/tom's frequency-filter fallback stays.

**Past known issue (fixed):** this stage used to call the CPJKU
"music-source-restoration" project (a HiFi++ GAN) via
`restoration.mixture_inference.create_mixture_system` -- a module that never
existed in that repo, which also has no setup.py/pyproject.toml (not
pip-installable at all, just a training codebase). That import always failed
and silently fell back to spectral filtering, regardless of what was
installed. Replaced with the denoise + de-reverb models above, which are
real, tested, and use infrastructure already proven in this pipeline.

**Performance:** ~12-17 min per track (restoration no longer a mandatory
stage -- see below; vocals run two models in sequence, and the
instruments side now runs two Demucs passes in sequence too, since Stage 1
is bottlenecked by whichever of the two threads finishes last)
- Stage 1 (parallel vocals ensemble + instruments): ~10-13 min
- Stage 2 (drum splitting + filtering): ~2-3 min
- Stage 3 (assembling stems): seconds

## Features & Components

### Multi-Engine Stem Separation (9 stems)
**Models (best-in-class, chosen by listening test where no reliable SDR exists):**
- **Vocal-model ensemble** (Mel-Band RoFormer by becruily + BS-RoFormer 12.1 dB SDR, averaged) → lead vocals
- **Demucs htdemucs_6s** (9.5 dB SDR) → guitar, piano, other (only model that separates these at all)
- **Demucs hdemucs_mmi** (bass 12.2 dB / drums 9.6 dB SDR) → bass, and the drums stem that feeds kick/snare/hihat/tom below
- **MDX23C DrumSep** (SOTA) → kick, snare (from the hdemucs_mmi drums stem)
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
  (`extra_vocals_becruily`, `extra_instrumental_becruily`,
  `extra_vocals_bs_roformer`, `extra_instrumental_bs_roformer`), and
  Demucs' own unused vocals (`extra_vocals_demucs`)
- `vocals_original.wav`/`vocals_restored.wav` if vocals' restoration has
  been toggled (see Restoration above) -- the only restorable stem
  (`RESTORABLE_STEMS` in server.py), so it's also the only one that ever
  gets an `_original.wav` backup in the first place
- ACID chunks embedded on every file (BPM/key for DAW auto-detect)
- No more separate "original/" vs "processed/" folders -- a song only has
  one current state at a time (whatever's currently active, as-is or
  target-processed), so there was never a real second version to split
  out; the two folders used to serve the exact same file under both labels

**`/api/download-unaligned-stems`** - pre-alignment backup (on-demand,
separate from the above): re-separates the pre-alignment WAV for whichever
song(s) used "align", since that source is never separated automatically.

## Beatgrid & Kick-Transient Detection

**Problem this solves:** a vague/drum-less intro (ambient pad, quiet build-up,
DJ-edit buildup section) can anchor `beat_anchor`/`ticks[0]` to whatever weak
content sits at the start of the track, corrupting cross-song phase
alignment (`snap_to_reference`) and self-alignment (`align_beatgrid`) alike.
`mashup_engine.py`'s `_detect_kick_candidates`/`_detect_beats_essentia` skip
past that intro before analysis, then add the trimmed offset back onto every
tick so the rest of the app never has to know the intro was skipped.

**Detection (`_detect_kick_candidates`):** low-pass filters the first
`KICK_SEARCH_MAX_SEC` (90s) of the track below ~150 Hz, runs onset
detection, then validates each onset by **crest factor** (peak/RMS in a
`KICK_CREST_WINDOW_SEC` window, `KICK_CREST_FACTOR_MIN = 4.0`) rather than
trusting the onset-strength envelope's own scale directly -- a loud/
overdriven sub-bass drone in the intro can skew that envelope's baseline
unpredictably (tested: an early sub-drop and a real groove start scored
near-identical envelope-relative strength), while crest factor stays
reliable since a genuine kick's energy is a short spike far above its
surroundings, whereas a sustained drone -- however loud -- keeps peak and
RMS close together. Consecutive qualifying onsets closer than
`KICK_CANDIDATE_MIN_GAP_SEC` (2.0s) are grouped into one "run" (a normal
song with drums from the start is one continuous run, not several
near-duplicate candidates), keeping each run's first onset time and how
many onsets it contains (`run_length`).

**Candidate ranking:** when more than one candidate qualifies (e.g. an early
sub-bass drop before the track's real downbeat), candidates are ranked by
`run_length` FIRST, with Essentia's own `RhythmExtractor2013` confidence
used only to break an exact tie. Validated against 38 real tracks
(`validate_pipeline.py`) -- an earlier version did the reverse (confidence
primary) and failed badly: confidence mostly reflects how periodic the BULK
of the analyzed audio is, which barely differs between candidates sharing
the same downstream track. One case made this unambiguous: a candidate with
`run_length` 85 (an overwhelmingly dominant, sustained groove) lost to one
with `run_length` 4 purely on a confidence difference. Ranking by
`run_length` first also cuts cost, since only candidates tied for the top
`run_length` are ever run through the (expensive) extractor at all --
usually just one, instead of up to 3. The Librosa fallback path
(`_pick_kick_offset`, used by `analyze_track`/`analyze_track_and_key` only
if Essentia fails entirely) uses the same `run_length`-primary ranking, for
the same reason -- tested there too: comparing candidates by their own
`beat_track()` tempo estimate against a reference BPM didn't discriminate at
all (an isolated pre-groove transient and the real groove start produced
the *identical* tempo estimate), since tempo estimation is deliberately
robust to exactly where a mostly-periodic window starts.

**Pre-roll (`KICK_PRE_ROLL_SEC = 0.35`):** cutting the analysis audio
exactly on the kick's transient gives the beat tracker's first frame no
quiet lead-in to compute its own onset/spectral-flux value against, which
can bias that very first detected beat (used as `beat_anchor`). For tracks
under `KICK_PRE_ROLL_REFINE_MAX_BPM` (100 BPM), the pre-roll is refined
after a first pass to one beat's duration (`60/bpm`, clamped to
`KICK_PRE_ROLL_MIN_SEC`-`KICK_PRE_ROLL_MAX_SEC`, i.e. 0.2-0.5s) -- gated to
slow tracks specifically because `60/bpm` diverges from the 0.35s default
for most real music (not just outliers), so refining unconditionally would
double the extra Essentia call on most songs for a benefit that's clearest
at slow tempo.

**Past known issue (fixed):** `_warp_beats_to_grid` (shared core of
`align_beatgrid`/`snap_to_reference`) used to assume `ticks[0]` always maps
to grid index 0 -- true before kick-trimming existed (Essentia's own first
detected beat was rarely more than a couple seconds in), but once
kick-trimmed detection could put `ticks[0]` tens of seconds into the track,
that assumption produced a nonsensical tens-of-seconds "correction" and
would have asked RubberBand to crush that whole span down to nothing when
snapping to another song whose own anchor sits near 0. Fixed by finding
which grid index (`k0`) `ticks[0]` is actually closest to and enumerating
from there -- since the target grid is periodic, any integer `k0` lands on
the same absolute-time grid (phase-locking between songs is unaffected),
so this only minimizes distortion instead of introducing one.

## Startup: Dependency Checks & Model Prefetch

At server startup (`server.py`, before `serve(app, ...)` -- the server
starts accepting connections immediately, on its own background thread) the
app checks its dependencies and pre-downloads every model it needs, so the
first real separation a user runs never stalls on a multi-GB download, and
Song 1/Song 2's parallel per-song processing threads can never race each
other into corrupting a model file (see below).

**`MashupEngine.check_environment()`** — checks ffmpeg/demucs/
rubberband presence, GPU availability, and whether `audio_separator`
imports; each check is marked `required` (ffmpeg, demucs -- nothing works
without them) or optional. A failed optional check carries a short
`warning_label` ("slow processing", "align/snap disabled", "legacy mode
only") for the UI to show instead of a bare, alarming "error" for something
that isn't actually broken, just reduced-capability.

**`MashupEngine.prefetch_models()`** — fetches all 8 models (5
audio-separator + `htdemucs`/`htdemucs_6s`/`hdemucs_mmi`) **in parallel** (one thread
each, via `ThreadPoolExecutor`) -- they're independent files, nothing about
them requires serializing. Byte-level download progress is captured by
transparently swapping in for the exact `tqdm` progress bar
audio-separator's own download code already creates for the terminal (a
thread-local context, since multiple models download concurrently and the
swapped-in class is a single shared module attribute -- installed once for
the whole batch, not per-model), throttled to ~10 reports/second (audio-
separator's download loop calls `update()` once per 8KB chunk -- untouched,
a ~900MB model would be 100,000+ calls). `_model_load_locks` (per-model-
**filename**, not one global lock) guards every `Separator(...).load_model()`
call site against the cross-song race (`audio_separator`'s own
`download_file_if_not_exists()` writes straight to the final path with no
locking or atomic rename of its own -- two threads seeing "not cached yet"
at the same moment would both start writing the same file); per-filename
rather than one lock so prefetching different models in parallel isn't
serialized by the very same lock meant to stop two threads racing on the
*same* file.

**Corrupted/incomplete download detection:** `AUDIO_SEPARATOR_MODEL_SIZES`
holds the exact known-good byte size for each of the 5 audio-separator
checkpoints; a cached file whose size doesn't match is treated as a
truncated/corrupted download (deleted and re-fetched) rather than silently
handed to a real separation later, where it'd surface as a much more
confusing failure. This only matters for audio-separator's own cache --
Demucs' weights come from HuggingFace Hub (`~/.cache/huggingface`), which
downloads to a `.incomplete` temp file and atomically renames it on
completion, so an interrupted download there can never leave a corrupted
final blob in the first place. (Size-only checking has a known gap: it
can't catch corruption that happens to leave the exact right file size --
confirmed directly during development, from an unrelated file-write
collision -- so it's a cheap safety net for the common "download got
killed" case, not a full integrity guarantee.)

**Frontend (`StartupOverlay.jsx`):** polls `/api/startup-status`, which
reports every check/model's live status (`queued` → `checking` →
`cached`/`downloading` → `done`/`error`) -- the full list is populated as
`queued` immediately (`MashupEngine.all_startup_items()`) so the GUI shows
every item at once rather than one at a time as they're reached. A small
per-item pacing delay (`_STARTUP_CHECK_DISPLAY_DELAY_SEC` in server.py,
0.5s) after each "checking" report keeps that state visibly on-screen
briefly, since the checks themselves are otherwise near-instant. The
overlay blocks the rest of the UI until every `required` item resolves,
then shows an OK button (no auto-dismiss) -- optional issues (no GPU,
rubberband missing) are shown live but never hold that up. Audio-separator
models additionally get a real progress bar (from the tqdm bridge above);
Demucs models just show "downloading…" (their two files are small enough,
~53-81MB, that a percentage wasn't worth the added complexity of hooking
HuggingFace Hub's own progress reporting).

## Development Notes

- **32-bit float WAV end-to-end**: every intermediate/output WAV this app
  writes (upload conversion, beatgrid warp, time-stretch/pitch-shift passes,
  drum-frequency splitting, final render) uses `pcm_f32le`, not ffmpeg's
  16-bit default -- matches the float32 precision every model here (Demucs,
  the vocal RoFormers, MDX23C DrumSep) already computes in internally, so no
  intermediate hop re-quantizes. Demucs gets this via its own `--float32`
  CLI flag. audio-separator needs more than just `use_soundfile=True`:
  `CommonSeparator.write_audio_soundfile` has its own int16-hardcoding bug
  for the common (C-contiguous) array case, so
  `MashupEngine._ensure_audio_separator_float_output()` patches it to write
  float32 (still applying the same peak-normalization its default pydub
  path would have). Bit depth has no effect on separation time -- the
  models convert to float32 tensors regardless of source precision; the
  only cost is ~2x larger files on disk and in downloads (4 bytes/sample
  vs. 16-bit's 2).
- BPM detection and stem separation run in background threads
- Multi-engine mode uses parallel GPU processing (Stage 1: vocal-model ensemble + Demucs x2)
- Stem separation pipeline: ~12-17 min per track (quality prioritized;
  restoration is opt-in per stem afterward, not part of this)
  - Stage 1 (parallel): ~10-13 min
  - Stage 2 (drum splitting): ~2-3 min
  - Stage 3 (assembling stems): seconds
- All generated audio goes under `Audio/` (git-ignored): uploads/aligned WAVs,
  `Audio/stems/<timestamp>/`, `Audio/renders/`, and download ZIPs
- Separation results are cached in `separated_stems/<hash>/` (git-ignored);
  `/api/cleanup` clears both this and `Audio/`, but leaves the downloaded
  model weights alone (not per-song generated data) -- audio-separator's 5
  models under `~/.cache/audio-separator-models` (~3.4GB) and Demucs'
  `htdemucs`/`htdemucs_6s`/`hdemucs_mmi` under `~/.cache/huggingface`
  (~294MB); see
  "Startup: Dependency Checks & Model Prefetch" for how these get fetched
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
