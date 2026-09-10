# DualSync Pro

**AI-Powered Dual-Song Real-Time Audio Mixer with Beatmatching & Key Transposition**

![React](https://img.shields.io/badge/React-18-blue?style=flat-square)
![Flask](https://img.shields.io/badge/Flask-Web_API-orange?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

DualSync Pro is a modern web application for creating audio mashups. Load two songs side-by-side, automatically detect their BPM and key, beatmatch and transpose them to a common key, and mix the results in real-time with independent stem controls. All audio processing is powered by AI (Demucs for stem separation, Librosa for BPM detection, Essentia for key detection) and professional audio tools (FFmpeg for beatmatching and pitch-shifting).

---

## ✨ Features

### Real-Time Mixing
- **Dual-song mixer** — load two MP3s into independent slots with synchronized playback
- **HTML5 audio synchronization** — first song's timeline controls both songs for seamless mixing
- **Independent stem volumes** — control Vocals, Drums, Bass, and Other separately for each song (0-100% sliders)
- **Crossfader** — blend between Song 1 and Song 2 in real-time
- **Auto-loop playback** — automatically restart at track end during playback

### Audio Analysis & Processing
- **AI Stem Separation** — isolate vocals, drums, bass, and other instruments using Demucs
- **Multi-Pass BPM Detection** — analyze multiple sections of each song using Librosa, take median for accuracy
- **Smart Key Detection** — identify song key using Librosa chroma analysis (primary) with Essentia fallback
- **Beatmatching with Verification** — align Song 2 to Song 1 (or both to target BPM) with automatic multi-pass correction
  - Pass 1: FFmpeg tempo-stretching (stable baseline)
  - Pass 2+: Optional RubberBand for higher quality (if available)
  - Automatic re-analysis after each pass to verify convergence (±10 BPM tolerance)
- **Key Transposition** — pitch-shift tracks to match target key
- **Intelligent Key Recommendation** — algorithm finds best compromise key between both songs, minimizing maximum individual transposition

### Downloads & Exports
- **FLAC format with metadata** — lossless audio with embedded TITLE, BPM, INITIALKEY tags
- **Measured BPM in filenames** — actual output BPM shown in filenames (not target)
- **Smart naming convention:**
  - Original stems: `songname-[detected-bpm]-[key]-[stem].flac`
  - Processed stems: `songname-[measured-bpm]-[key]-[stem].flac`
  - Manual BPM override: `songname-[target]-manual-[measured]-[key]-[stem].flac`
- **9 downloadable files** (all as FLAC with verified output BPM):
  - Original Song 1 stems (4 stems)
  - Original Song 2 stems (4 stems)
  - Beatmatched + transposed stems (4 stems with verified output BPM)
  - Final mix (all stems combined with volume settings + crossfader)
- **ZIP archives** — organized downloads with accurate naming

### User Experience
- **Explicit Process buttons** — Process BPM and Process Key buttons only enabled when values change from last processed state
- **Real-time progress indication** — animated progress bars during beatmatching and key transposition
- **Processing state display** — visual feedback showing which stems version is currently being used
- **Drag-and-drop upload** — intuitive file loading with visual feedback
- **Responsive design** — works on desktop browsers
- **Locked controls during processing** — all mixing controls disabled while beatmatching or transposition is in progress

---

## How It Works

### 1. Upload & Analyze
When you upload an MP3 file:
1. **Stem Separation** — Demucs AI model isolates 4 tracks: Vocals, Drums, Bass, Other
2. **BPM Detection** — Librosa analyzes 2-3 sections of the song, calculates median BPM
3. **Key Detection** — Librosa chroma analysis identifies the song's harmonic key
4. Results are displayed and ready for mixing

Both songs are processed in parallel (independent uploads).

### 2. Prepare for Mixing
Before mixing, you can:
- **Override detected BPM** — manually enter a target BPM if auto-detection is wrong
- **Override detected Key** — manually select a different key from dropdown
- **Click "Recommend"** — algorithm finds the best compromise key between both songs

### 3. Process & Beatmatch
When you click **"Process BPM"** button:
1. **Pass 1** — FFmpeg applies initial tempo-stretching to Song 2 (or both if target BPM set)
2. **Verification** — Librosa re-analyzes output BPM
3. **Convergence Check** — if output is within ±10 BPM of target, done; otherwise continue
4. **Pass 2+** — Optional RubberBand processing for refinement (if available)
5. **Final Verification** — measure output BPM and lock in processed stems
6. Progress bar animates during processing, actual processing state updates when complete

The **"Process BPM" button is disabled** until you change the target BPM from the last processed value.

### 4. Transpose & Match Keys
When you click **"Process Key"** button:
1. **Pitch-shift all stems** to match target key using FFmpeg asetrate
2. **Update metadata** with new key value
3. Progress bar animates during processing

The **"Process Key" button is disabled** until you change the target key from the last processed value.

### 5. Mix in Real-Time
After processing:
- Adjust stem volumes for Song 1 and Song 2 independently
- Use crossfader to blend between songs
- Play/Pause and scrub through timeline
- Listen to beatmatched, transposed mix in real-time

### 6. Download
Download your results:
- **Original Stems** — stems at auto-detected BPM/Key (before beatmatching)
- **Processed Stems** — beatmatched + transposed stems with verified output BPM
- **Final Mix** — all stems combined with your volume settings and crossfader position

All files are FLAC format with embedded metadata (TITLE, BPM, INITIALKEY).

---

## Architecture

### Frontend (React)
- **React 18** — interactive UI for real-time mixing
- **Vite** — fast development server and bundler
- **HTML5 Audio API** — synchronized playback of dual songs
- **DualMixer.jsx** — main component managing:
  - Upload and file handling
  - BPM/Key processing state and UI
  - Stem volume sliders (per-song)
  - Crossfader control
  - Playback synchronization
  - Progress indication during processing

### Backend (Flask)
- **Flask** + Flask-CORS — REST API for audio processing
- **API Endpoints:**
  - `POST /api/separate-stems` — upload file → stem separation + BPM/key detection
  - `POST /api/process-stems` — beatmatch + transpose stems (via FFmpeg/RubberBand)
  - `POST /api/render-final-mix` — mix all stems into final FLAC with metadata
  - `POST /api/download-stems-zip` — download stems as FLAC in ZIP
  - `GET /api/download-file/<filename>` — download single audio file

### Audio Processing Core (Python)
- **Demucs** — AI stem separation (isolates vocals, drums, bass, other)
- **Librosa** — BPM detection (multi-pass median) and audio analysis
- **Essentia** — key detection (fallback if Librosa chroma unavailable)
- **FFmpeg** — tempo-stretching, pitch-shifting, final mix rendering, FLAC encoding
- **RubberBand** — optional higher-quality time-stretching (Pass 2+ only)
- **Mutagen** — FLAC metadata tagging

### File Structure
```
stem-mashup-pro/
├── frontend/                     # React app
│   ├── src/
│   │   ├── components/
│   │   │   ├── DualMixer.jsx    # Main mixing interface
│   │   │   └── ...
│   │   ├── styles/
│   │   └── App.jsx
│   ├── vite.config.js
│   └── package.json
├── api.py                        # Flask REST API endpoints
├── mashup_engine.py              # Audio processing core
├── requirements.txt              # Python dependencies
└── Audio/                        # Generated stems/mixes (git-ignored)
    ├── stems/[timestamp]/        # Original stems per upload
    ├── processed/[timestamp]/    # Beatmatched stems per session
    └── ...
```

---

## Clone, Install & Update

### Prerequisites
- **Python 3.10+** (3.12+ recommended)
- **Node.js 16+** (for React frontend)
- **FFmpeg** (for audio processing)
- **Internet connection** (first run downloads Demucs AI model ~500MB)

### Clone the Repository

```bash
git clone https://github.com/laudaniels/Stem-Mashup-Pro.git
cd Stem-Mashup-Pro
```

### Install Python Backend

**Create and activate virtual environment:**

```bash
# macOS/Linux
python3 -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1

# Windows (Command Prompt)
python -m venv .venv
.venv\Scripts\activate.bat
```

**Install Python dependencies:**

```bash
pip install -r requirements.txt
```

This installs:
- Flask & Flask-CORS (web server)
- Librosa (BPM detection)
- Essentia (key detection fallback)
- Demucs (stem separation)
- Mutagen (FLAC metadata)
- (Optional) RubberBand for higher-quality time-stretching

### Install Frontend Dependencies

```bash
cd frontend
npm install
cd ..
```

### Install System Dependencies

**FFmpeg:**
- **macOS:** `brew install ffmpeg`
- **Ubuntu/Debian:** `sudo apt-get install ffmpeg`
- **Fedora:** `sudo dnf install ffmpeg`
- **Windows:** Download from [ffmpeg.org](https://ffmpeg.org/download.html) or `choco install ffmpeg`

**Verify FFmpeg installation:**
```bash
ffmpeg -version
```

**Optional: RubberBand (for higher-quality beatmatching)**
- **macOS:** `brew install rubberband`
- **Ubuntu/Debian:** `sudo apt-get install librubberband-dev`
- **Fedora:** `sudo dnf install rubberband-devel`
- **Windows:** Download from [breakfastquay.com](https://breakfastquay.com/rubberband/)

### Running the Application

**Start the Flask backend (Terminal 1):**

```bash
source .venv/bin/activate    # or .venv\Scripts\activate on Windows
python3 api.py
```

Output:
```
 * Running on http://127.0.0.1:5000
 * Press CTRL+C to quit
```

**Start the React frontend (Terminal 2):**

```bash
cd frontend
npm run dev
```

Output:
```
Local:   http://localhost:5173
```

Open `http://localhost:5173` in your browser.

### Update to Latest Version

**Pull latest code:**

```bash
git pull origin main
```

**Update Python dependencies:**

```bash
source .venv/bin/activate    # or .venv\Scripts\activate on Windows
pip install -r requirements.txt --upgrade
```

**Update Node dependencies:**

```bash
cd frontend
npm install
cd ..
```

**Restart both servers** (Stop with Ctrl+C and run again):
- Flask: `python3 api.py`
- React: `npm run dev` (from `frontend/` directory)

---

## Troubleshooting

### Demucs Model Download Fails
On first run, Demucs downloads a ~500MB AI model. This requires internet connection and may take 1-2 minutes.

**If stuck:**
```bash
# Manually download the model
python3 -c "from demucs.apply import load_model; load_model('htdemucs')"
```

### FFmpeg Not Found
Ensure FFmpeg is in your system PATH:
```bash
ffmpeg -version
```

If not found, install it (see System Dependencies above).

### Port Already in Use
- **Flask (5000):** Edit `api.py`, change `port=5000` to another port
- **React (5173):** Vite uses first available port, or edit `frontend/vite.config.js`

### "Can't enter BPM" or Input Frozen
This is fixed in the current version. Make sure you're on the latest branch:
```bash
git pull origin main
```

### RubberBand Build Fails
RubberBand is optional. If install fails, the app still works with FFmpeg (Pass 1). To skip RubberBand:
```bash
pip install -r requirements.txt --no-build-isolation
```

---

## Basic Workflow

1. **Upload Song 1** — drag-and-drop MP3 or click upload area
   - Automatic stem separation (4 stems)
   - BPM and key auto-detected
2. **Upload Song 2** — same as Song 1
   - Parallel processing (doesn't wait for Song 1)
3. **Review metadata** — check detected BPM/key for each song
4. **Optional overrides:**
   - Change target BPM (if auto-detection is wrong)
   - Change target key (from dropdown)
   - Click "Recommend" for best compromise key
5. **Click "Process BPM"** — beatmatch both songs (animated progress bar)
   - Wait for completion (progress bar → 100%)
6. **Click "Process Key"** — transpose to target key (animated progress bar)
   - Wait for completion (progress bar → 100%)
7. **Mix in real-time:**
   - Adjust stem volumes for each song
   - Use crossfader to blend between songs
   - Play/Pause and scrub timeline
8. **Download:**
   - Original Stems (ZIP with 8 FLAC files at detected BPM/Key)
   - Processed Stems (ZIP with 4 beatmatched+transposed FLAC files)
   - Final Mix (single FLAC file with all stems mixed at your volume settings)

---

## Key Concepts

### BPM Detection & Beatmatching
- **Detection:** Librosa analyzes 2-3 sections of each song, calculates median BPM
- **Beatmatching:** FFmpeg tempo-stretching aligns Song 2 to Song 1 (or both to target)
- **Verification:** Output BPM is measured after each pass; if off by >±10 BPM, another pass is applied
- **Filenames:** Show actual measured output BPM, not target BPM (e.g., if target=112, output might be 111.8)

### Key Recommendation Algorithm
- Takes Song 1 and Song 2's detected keys
- Finds the single key that minimizes the maximum transposition needed for either song
- Example: if Song 1 is C and Song 2 is F#, recommendation might be D (C→D +2 semitones, F#→D -4 semitones, max=4) instead of C (C→C 0, F#→C -5, max=5)
- Shows best compromise, not perfect for both songs

### Processing State
- **Stems Version Info** — displayed under the crossfader
- **Format:** "📦 Using stems from: Song 1 [E 129.2 BPM] + Song 2 [F# 103.4 BPM]"
- **Updates after:**
  - Stem separation completes (shows auto-detected values)
  - Beatmatching completes (shows measured output BPM)
  - Transposition completes (shows new key)

### Process Buttons
- **Process BPM button** — DISABLED until target BPM changes from last processed value
- **Process Key button** — DISABLED until target key changes from last processed value
- **All controls** — locked during processing (can't change volumes, crossfader, etc.)

---

## File Naming Examples

### Original Stems (Auto-Detected)
```
Part3-Venus-96-C-vocals.flac          # 96 BPM, Key C
Part3-Venus-96-C-drums.flac
Part3-Venus-96-C-bass.flac
Part3-Venus-96-C-other.flac
```

### Processed Stems (Beatmatched, Measured Output)
```
Part3-Venus-111.8-F-vocals.flac       # Actual measured output: 111.8 BPM, Key F
Part3-Venus-111.8-F-drums.flac
Part3-Venus-111.8-F-bass.flac
Part3-Venus-111.8-F-other.flac
```

### With Manual BPM Override
```
Part3-Venus-112-manual-111.8-measured-F-vocals.flac
# Target: 112 BPM, Measured output: 111.8 BPM, Key: F
```

---

## Output Directory Structure

All generated files are stored in `Audio/` folder with timestamps:

```
Audio/
├── stems/
│   ├── [timestamp]/                    # Original stems per upload
│   │   ├── Song1_vocals.wav
│   │   ├── Song1_drums.wav
│   │   ├── Song1_bass.wav
│   │   ├── Song1_other.wav
│   │   ├── Song2_vocals.wav
│   │   └── ... (Song 2 stems)
│
├── processed/
│   ├── [timestamp]/                    # Beatmatched stems per session
│   │   ├── vocals_beatmatched_transposed.wav
│   │   ├── drums_beatmatched_transposed.wav
│   │   ├── bass_beatmatched_transposed.wav
│   │   └── other_beatmatched_transposed.wav
│
└── [timestamp]_final_mix.flac          # Final stereo mix with metadata
```

Downloaded files are FLAC format with embedded tags:
- **TITLE:** Song name(s)
- **BPM:** Measured output BPM (after beatmatching)
- **INITIALKEY:** Target key (after transposition)

---

## Requirements

See `requirements.txt` for complete Python dependencies. Key packages:
- **Flask** — REST API server
- **librosa** — BPM detection and audio analysis
- **essentia** — key detection (fallback)
- **demucs** — AI stem separation
- **mutagen** — FLAC metadata tagging
- **pydub** — audio manipulation (optional)

React frontend requires Node.js 16+ with packages listed in `frontend/package.json`.

---

## System Requirements

- **Processor:** Modern CPU (Intel i5+ or AMD Ryzen 5+) recommended for real-time mixing
- **RAM:** 8GB minimum, 16GB recommended (stem separation uses ~2-4GB per song)
- **Storage:** 50GB+ free space (Demucs model + generated stems/mixes)
- **Network:** Internet required for initial Demucs model download (~500MB)

---

## Performance Notes

- **First run:** Demucs model downloads (~500MB, 1-2 minutes) and caches locally
- **Stem separation:** 2-3 minutes per song (depends on length and CPU)
- **Beatmatching:** 30 seconds to 2 minutes (depends on convergence)
- **Real-time mixing:** Smooth 60+ FPS on modern browsers
- **File downloads:** FLAC encoding adds 30-60 seconds per file

---

## Disclaimer

This tool is for educational and creative purposes. Only use audio files you own or have permission to use. The developer is not responsible for copyright violations or misuse.

**Stem separation is AI-powered and experimental.** Demucs does its best to isolate instruments, but results are not perfect. Use downloaded stems as a starting point, not as final-quality isolated tracks.

---

## Credits

- **AI Stem Separation:** [Demucs](https://github.com/facebookresearch/demucs) by Meta
- **Audio Analysis:** [Librosa](https://librosa.org/) and [Essentia](https://essentia.upf.edu/)
- **Audio Stretching:** [RubberBand](https://breakfastquay.com/rubberband/) by Breakfast Quay
- **Project Inspiration:** [Neon Mashup Studio](https://github.com/codewithpb11/neon-mashup-studio) by Pramit Bakski

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

**Last Updated:** 2026-09-09  
**Current Branch:** main (merged from feature/quality-improvements)
