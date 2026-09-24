"""Flask API for DualSync Pro"""
from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from pathlib import Path
import logging
import time

logging.basicConfig(level=logging.DEBUG)
logging.getLogger('numba').setLevel(logging.WARNING)

app = Flask(__name__, static_folder='frontend/dist', static_url_path='')
CORS(app)

BASE_DIR = Path(__file__).resolve().parent


# ===== Static Files =====
@app.route('/favicon.ico')
def favicon():
    """Return a simple 204 No Content for favicon requests"""
    return '', 204


@app.route('/')
def index():
    """Serve React frontend"""
    return send_from_directory('frontend/dist', 'index.html')


@app.route('/<path:filepath>')
def serve_files(filepath):
    """Serve static assets or fall back to React routing"""
    # Try serving from frontend dist
    dist_path = Path('frontend/dist') / filepath
    if dist_path.is_file():
        logging.info(f"Serving from dist: {filepath}")
        return send_from_directory('frontend/dist', filepath)

    # Fall back to index.html for React routing
    logging.info(f"File not found: {filepath}, serving index.html for React routing")
    return send_from_directory('frontend/dist', 'index.html')




# ===== API Routes =====
@app.route('/api/upload-audio', methods=['POST'])
def upload_audio():
    """Step 1: save the upload and convert it to WAV. Returns a reference the
    frontend holds onto while the user picks 'process as is' vs 'beat
    alignment' -- the heavier analyze/align/separate work happens in
    /api/process-song, once that choice is made."""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    try:
        import subprocess
        import time
        global _processing_state

        # Both songs can upload at once (threaded server), so only clear this
        # slot's own previous log lines -- wiping the whole shared log here
        # would erase the other song's still-relevant progress.
        slot_raw = request.form.get('slot')
        slot = int(slot_raw) if slot_raw not in (None, '') else None
        if slot is not None:
            prefix = f"[Song {slot + 1}]"
            _processing_state['logs'] = [m for m in _processing_state['logs'] if not m.startswith(prefix)]
        else:
            _processing_state['logs'] = []

        audio_dir = BASE_DIR / 'Audio'
        audio_dir.mkdir(exist_ok=True)

        original_path = audio_dir / file.filename
        file.save(str(original_path))
        add_log_message(f"📥 Uploading: {file.filename}", slot)

        # Convert to WAV (timestamp-prefixed so slot 0/1 uploading files with
        # the same name never collide, and so re-uploading the same filename
        # doesn't clobber a file the other song might still be using).
        add_log_message("🔄 Converting to WAV...", slot)
        wav_filename = f"{int(time.time() * 1000)}_{Path(file.filename).stem}.wav"
        wav_path = audio_dir / wav_filename
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(original_path), "-ar", "44100", str(wav_path)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(f"WAV conversion failed: {result.stderr[-500:]}")

        # Peak-normalize to 97% so both songs enter the pipeline (alignment,
        # analysis, separation) at a consistent, headroom-safe level rather
        # than whatever level they happened to be mastered at. Best-effort:
        # if it fails, the unnormalized WAV is left in place.
        add_log_message("📏 Normalizing peak level to 97%...", slot)
        from mashup_engine import MashupEngine
        MashupEngine().normalize_peak(str(wav_path), str(wav_path), target_peak=0.97)

        add_log_message("✅ Ready -- choose how to process this song", slot)

        return jsonify({
            'wav_filename': wav_filename,
            'filename': file.filename
        })
    except Exception as e:
        logging.error(f"Upload/conversion failed: {e}", exc_info=True)
        return jsonify({'error': f'Upload failed: {str(e)}'}), 500


@app.route('/api/process-song', methods=['POST'])
def process_song():
    """Step 2: the user has chosen 'as_is' or 'align' for the WAV produced
    by /api/upload-audio -- run that (optional) beatgrid alignment, then BPM
    and key detection, then Demucs stem separation."""
    data = request.json or {}
    wav_filename = data.get('wav_filename')
    filename = data.get('filename', wav_filename)
    slot = data.get('slot')
    mode = data.get('mode', 'as_is')
    # Only used for mode == 'snap': the OTHER song's already-known bpm/beat
    # anchor to warp this one onto (see /api/upload-audio's normalize step --
    # this runs on that same normalized WAV, before separation).
    reference_bpm = data.get('reference_bpm')
    reference_anchor = data.get('reference_anchor')

    if not wav_filename:
        return jsonify({'error': 'Missing wav_filename'}), 400

    try:
        import shutil
        import time

        audio_dir = BASE_DIR / 'Audio'
        file_path = audio_dir / wav_filename
        if not file_path.exists():
            return jsonify({'error': f'Uploaded WAV not found: {wav_filename}'}), 400

        from mashup_engine import MashupEngine
        engine = MashupEngine()

        # How much the initial beat-grid step (align or snap) actually
        # corrected -- None if neither ran or it failed, else {mean_ms, max_ms}
        # for the frontend to display alongside this song's detected info.
        grid_correction = None

        if mode == 'align':
            add_log_message("🎯 Aligning beatgrid (correcting tempo drift)...", slot)
            try:
                aligned_path = audio_dir / f"aligned_{file_path.stem}.wav"
                _, orig_bpm, _orig_anchor, mean_ms, max_ms = engine.align_beatgrid(str(file_path), str(aligned_path))
                file_path = aligned_path
                grid_correction = {'mean_ms': round(mean_ms, 1), 'max_ms': round(max_ms, 1)}
                add_log_message(f"✅ Beatgrid aligned (was {orig_bpm:.1f} BPM with drift, mean correction {mean_ms:.1f}ms)", slot)
            except Exception as e:
                logging.error(f"Beatgrid alignment failed: {e}", exc_info=True)
                add_log_message(f"⚠️ Beatgrid alignment failed, continuing without it: {e}", slot)

        elif mode == 'snap':
            if not reference_bpm or reference_anchor is None:
                add_log_message("⚠️ Snap beat requested but no reference song info was provided, continuing without it", slot)
            else:
                add_log_message(f"🧲 Snapping beat grid to the other song ({reference_bpm:.1f} BPM)...", slot)
                try:
                    snapped_path = audio_dir / f"snapped_{file_path.stem}.wav"
                    _, orig_bpm, _orig_anchor, mean_ms, max_ms = engine.snap_to_reference(
                        str(file_path), str(snapped_path), float(reference_bpm), float(reference_anchor)
                    )
                    file_path = snapped_path
                    grid_correction = {'mean_ms': round(mean_ms, 1), 'max_ms': round(max_ms, 1)}
                    add_log_message(f"✅ Snapped to reference beat grid (was {orig_bpm:.1f} BPM, mean correction {mean_ms:.1f}ms)", slot)
                except Exception as e:
                    logging.error(f"Beat-grid snap failed: {e}", exc_info=True)
                    add_log_message(f"⚠️ Beat-grid snap failed, continuing without it: {e}", slot)

        # Get BPM and key (combined single-pass analysis with 5-pass BPM strategy)
        add_log_message("🔍 Analyzing BPM and Key (5-pass detection)...", slot)
        bpm, beat_anchor, key, scale = engine.analyze_track_and_key(str(file_path))
        key_name = engine._key_to_note(key) if key >= 0 else "Unknown"

        add_log_message(f"✅ Detected: {bpm:.1f} BPM, {key_name} key", slot)

        # Check if multi-engine mode is enabled (via env var or config)
        import os
        use_multi_engine = os.getenv('DUALSYNC_MULTI_ENGINE', 'false').lower() == 'true'

        if use_multi_engine:
            add_log_message("🚀 Separating stems using multi-engine pipeline (Mel-Band RoFormer + BS-RoFormer + HiFi++)...", slot)
            add_log_message("  Stage 1: Parallel vocal extraction + 6-stem separation", slot)
            add_log_message("  Stage 2: Drum splitting into kick/snare/hihat/tom", slot)
            add_log_message("  Stage 3: Optional HiFi++ GAN restoration", slot)
        else:
            add_log_message("🔊 Separating stems using Demucs AI...", slot)

        stem_dict = engine.separate_stems([str(file_path)], use_multi_engine=use_multi_engine)[0]

        # Copy stems to a simple location for serving
        serve_dir = audio_dir / 'stems'
        serve_dir.mkdir(exist_ok=True)

        # Use timestamp to avoid conflicts
        timestamp = str(int(time.time() * 1000))
        session_dir = serve_dir / timestamp
        session_dir.mkdir(exist_ok=True)

        stems = {}
        add_log_message("📦 Copying stems to server...", slot)
        for stem_name, stem_path in stem_dict.items():
            if Path(stem_path).exists():
                # Copy to serve directory
                dest_path = session_dir / f"{stem_name}.wav"
                shutil.copy2(stem_path, str(dest_path))
                stems[stem_name] = f"/api/audio/{timestamp}/{stem_name}.wav"
                add_log_message(f"  ✅ {stem_name.capitalize()}", slot)
            else:
                add_log_message(f"  ⚠️ Stem not found: {stem_name}", slot)

        if not stems:
            raise Exception("No stems were separated successfully")

        add_log_message("✨ Stem separation complete!", slot)

        return jsonify({
            'stems': stems,
            'bpm': round(bpm, 1),
            'beat_anchor': beat_anchor,
            'key': key_name,
            'scale': scale,
            'filename': filename,
            # The WAV that was ACTUALLY analyzed and separated (the aligned
            # copy if mode=='align', otherwise the plain converted WAV) --
            # /api/process-stems must reprocess from this, not the raw
            # original upload, or it'll ignore alignment entirely and can
            # reintroduce MP3-decode/WAV timing mismatches.
            'source_wav_filename': file_path.name,
            'timestamp': timestamp,
            'grid_correction': grid_correction
        })
    except Exception as e:
        logging.error(f"Stem separation failed: {e}", exc_info=True)
        return jsonify({'error': f'Separation failed: {str(e)}'}), 500


@app.route('/api/audio/<path:filepath>')
def serve_audio(filepath):
    """Serve audio files"""
    audio_dir = BASE_DIR / 'Audio' / 'stems'
    file_path = audio_dir / filepath

    # This route is on the hot path for up to 14 concurrent streaming <audio>
    # elements, each issuing many range requests over the course of playback.
    # Logging on every request here (this used to be 4-6 log lines each,
    # including a duplicate exists() stat) contends for Python's logging lock
    # under that concurrency and was contributing to stems randomly stalling.
    if not file_path.exists():
        logging.error(f"Audio file not found: {file_path}")
        return jsonify({'error': 'File not found'}), 404

    return send_from_directory(str(audio_dir), filepath, mimetype='audio/wav')


# ===== Audio Stats =====
@app.route('/api/audio-stats', methods=['GET'])
def get_audio_stats():
    """Get audio files count and total size"""
    try:
        audio_dir = BASE_DIR / 'Audio'
        if not audio_dir.exists():
            return jsonify({'file_count': 0, 'total_size_mb': 0, 'total_size_formatted': '0 MB'})

        file_count = 0
        total_size = 0

        for file_path in audio_dir.rglob('*'):
            if file_path.is_file():
                file_count += 1
                total_size += file_path.stat().st_size

        total_size_mb = total_size / (1024 * 1024)
        total_size_formatted = f"{total_size_mb:.1f} MB" if total_size_mb >= 1 else f"{total_size / 1024:.1f} KB"

        return jsonify({
            'file_count': file_count,
            'total_size_mb': round(total_size_mb, 2),
            'total_size_formatted': total_size_formatted
        })
    except Exception as e:
        logging.error(f"Stats error: {e}", exc_info=True)
        return jsonify({'file_count': 0, 'total_size_mb': 0, 'total_size_formatted': '0 MB'})


# ===== Processing =====
# Both songs can be processed at once (threaded server), so status/progress
# live per-slot -- a single shared 'progress'/'status' would let one song's
# updates clobber the other's while both are in flight.
def _new_slot_state():
    return {'status': None, 'progress': 0, 'current_step': ''}


_processing_state = {
    'slots': {0: _new_slot_state(), 1: _new_slot_state()},
    'logs': [],  # Real-time log messages, tagged "[Song N] ..."
    'steps': [
        '📥 Loading original file',
        '🎵 Beatmatching to target BPM',
        '🎼 Transposing to target key',
        '🔊 Separating stems',
        '🥁 Splitting drums (auto)',
        '📦 Copying stems',
        '✅ Complete'
    ]
}


def add_log_message(message, slot=None):
    """Add a message to the processing log, tagged by which song slot it's for.

    Both songs can be uploading/processing at once, so untagged messages would
    be ambiguous once merged into the single shared log list the frontend polls.
    """
    global _processing_state
    tagged = f"[Song {slot + 1}] {message}" if slot is not None else message
    if len(_processing_state['logs']) > 50:  # Keep last 50 messages
        _processing_state['logs'].pop(0)
    _processing_state['logs'].append(tagged)
    logging.info(tagged)


def _set_slot_state(slot, **fields):
    """Update this slot's own progress/status without touching the other slot's."""
    global _processing_state
    _processing_state['slots'].setdefault(slot, _new_slot_state())
    _processing_state['slots'][slot].update(fields)


_VALID_KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _tag_stem_wav(engine, stem_file, dest_path, song_name, bpm, key, stem_label):
    """Copy stem_file to dest_path and embed tempo/key info a DAW can
    actually use: an ACID chunk (what FL Studio/Logic/Cubase/Reaper/etc.
    read on import) plus basic ID3-in-WAV tags for players/taggers. Returns
    True on success (dest_path is written either way; tagging failures are
    logged and swallowed so a bad tag never blocks the download)."""
    import shutil
    shutil.copy2(str(stem_file), str(dest_path))

    bpm_str = str(bpm).split('-')[0] if isinstance(bpm, str) else str(bpm)
    try:
        numeric_bpm = float(bpm_str)
    except (ValueError, TypeError):
        numeric_bpm = None

    if numeric_bpm:
        try:
            engine.write_acid_chunk(str(dest_path), numeric_bpm, key if key in _VALID_KEY_NAMES else None)
        except Exception as acid_err:
            logging.error(f"ACID chunk write failed for {dest_path.name}: {acid_err}")

    try:
        from mutagen.wave import WAVE
        from mutagen.id3 import TIT2, TPE1, TBPM, TKEY, COMM
        audio = WAVE(str(dest_path))
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TIT2(encoding=3, text=f'{song_name} ({stem_label})'))
        audio.tags.add(TPE1(encoding=3, text=song_name))
        if numeric_bpm:
            audio.tags.add(TBPM(encoding=3, text=str(int(round(numeric_bpm)))))
        audio.tags.add(TKEY(encoding=3, text=str(key)))
        audio.tags.add(COMM(encoding=3, lang='eng', desc='', text=f'{stem_label} stem - DualSync Pro'))
        audio.save()
    except Exception as tag_err:
        logging.error(f"WAV tagging failed for {dest_path.name}: {tag_err}")

    return True


@app.route('/api/process-stems', methods=['POST'])
def process_stems():
    """Process FULL SONG (BPM + Key), then separate into stems"""
    global _processing_state
    data = request.json
    slot = (data or {}).get('slot', 0)
    try:
        source_bpm = data.get('source_bpm')
        target_bpm = data.get('target_bpm')
        source_key = data.get('source_key')
        target_key = data.get('target_key')
        timestamp = data.get('timestamp')
        filename = data.get('filename')
        source_wav_filename = data.get('source_wav_filename')

        # Both songs can process at once -- only clear this slot's own
        # previous log lines, not the other song's.
        prefix = f"[Song {slot + 1}]"
        _processing_state['logs'] = [m for m in _processing_state['logs'] if not m.startswith(prefix)]

        import shutil
        from pathlib import Path

        add_log_message(f"🎯 Processing: BPM {source_bpm}→{target_bpm}, Key {source_key}→{target_key}", slot)

        if not timestamp or not filename:
            add_log_message(f"❌ Missing timestamp or filename", slot)
            return jsonify({'error': 'Missing timestamp or filename'}), 400

        # Reprocess from the SAME WAV that produced the currently-loaded
        # stems (the aligned copy if the song used beatgrid alignment,
        # otherwise the plain converted WAV) -- not the raw original upload.
        # Falling back to `filename` (the raw upload) only for old sessions
        # that never got a source_wav_filename.
        audio_dir = BASE_DIR / 'Audio'
        original_file = audio_dir / (source_wav_filename or filename)
        if not original_file.exists():
            add_log_message(f"❌ Original file not found", slot)
            return jsonify({'error': 'Original file not found'}), 400

        from mashup_engine import MashupEngine
        engine = MashupEngine()

        # Update progress state
        _set_slot_state(slot, status='processing', progress=10, current_step=_processing_state['steps'][0])
        add_log_message("📥 Loading original song...", slot)

        # Step 1: Process the FULL SONG first
        processed_song = audio_dir / f"processed_{timestamp}_{Path(filename).stem}.wav"

        measured_bpm = None
        measured_key_name = None
        # Apply BPM beatmatch if needed
        current_input = original_file
        if source_bpm and target_bpm and float(source_bpm) != float(target_bpm):
            _set_slot_state(slot, progress=20, current_step=_processing_state['steps'][1])
            add_log_message(f"🎵 Beatmatching: {source_bpm}→{target_bpm} BPM...", slot)
            success, measured_bpm = engine.time_stretch_audio(str(current_input), str(processed_song), float(target_bpm), float(source_bpm))
            if success:
                current_input = processed_song
                add_log_message(f"✅ Beatmatched to {measured_bpm:.1f} BPM", slot)
            else:
                add_log_message(f"⚠️ Beatmatch failed, continuing with original", slot)
                measured_bpm = None

        # Apply Key transpose if needed
        _set_slot_state(slot, progress=30, current_step=_processing_state['steps'][2])
        if source_key and target_key and source_key != target_key:
            keys = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
            source_idx = keys.index(source_key) if source_key in keys else -1
            target_idx = keys.index(target_key) if target_key in keys else -1

            if source_idx >= 0 and target_idx >= 0:
                semitones = target_idx - source_idx
                if semitones > 6:
                    semitones -= 12
                if semitones < -6:
                    semitones += 12

                transposed_song = audio_dir / f"transposed_{timestamp}_{Path(filename).stem}.wav"
                add_log_message(f"🎼 Transposing: {semitones} semitones ({source_key}→{target_key})...", slot)
                success, measured_key = engine.pitch_shift_audio(str(current_input), str(transposed_song), semitones, source_idx)
                if success:
                    current_input = transposed_song
                    measured_key_name = engine._key_to_note(measured_key) if measured_key >= 0 else target_key
                    add_log_message(f"✅ Transposed to {measured_key_name}", slot)
                else:
                    add_log_message(f"⚠️ Transpose failed, continuing", slot)

        # Step 2: Now separate stems from the PROCESSED full song (includes auto drum splitting)
        _set_slot_state(slot, progress=50, current_step=_processing_state['steps'][3])
        add_log_message(f"🔊 Separating stems from processed song...", slot)
        stem_dict = engine.separate_stems([str(current_input)])[0]

        # Copy processed stems to serve directory
        stems_dir = BASE_DIR / 'Audio' / 'stems' / timestamp
        stems_dir.mkdir(parents=True, exist_ok=True)

        processed_stems = {}

        # Copy PROCESSED stems to serve directory (now includes kick, snare, hihat, tom)
        _set_slot_state(slot, progress=70, current_step=_processing_state['steps'][5])
        add_log_message("📦 Copying processed stems...", slot)
        for stem in stem_dict.keys():
            stem_path = stem_dict.get(stem)
            if stem_path and Path(stem_path).exists():
                # Copy processed stem to serve directory
                dest_path = stems_dir / f"{stem}.wav"
                shutil.copy2(str(stem_path), str(dest_path))
                processed_stems[stem] = f"/api/audio/{timestamp}/{stem}.wav"
                add_log_message(f"  ✅ {stem.capitalize()}", slot)
            else:
                add_log_message(f"  ⚠️ {stem} not found", slot)

        if not processed_stems:
            add_log_message("❌ No stems were processed", slot)
            _set_slot_state(slot, progress=0, status='error')
            return jsonify({'error': 'Processing failed'}), 500

        _set_slot_state(slot, progress=100, current_step=_processing_state['steps'][6], status='success')
        add_log_message(f"✨ Processing complete!", slot)
        return jsonify({
            'status': 'success',
            'processed_stems': processed_stems,
            'measured_bpm': measured_bpm,
            'target_bpm': target_bpm,
            'source_bpm': source_bpm,
            'measured_key': measured_key_name,
            'target_key': target_key
        })
    except Exception as e:
        logging.error(f"Process error: {e}", exc_info=True)
        _set_slot_state(slot, progress=0, status='error')
        return jsonify({'error': f'Processing failed: {str(e)}'}), 500


@app.route('/api/split-drums', methods=['POST'])
def split_drums():
    """Split drum stem into kick, snare, hi-hat, and toms"""
    data = request.json
    try:
        from mashup_engine import MashupEngine
        import shutil

        drum_path = data.get('drum_path')
        timestamp = data.get('timestamp')

        if not drum_path or not Path(drum_path).exists():
            return jsonify({'error': 'Drum stem file not found'}), 400

        engine = MashupEngine()
        audio_dir = BASE_DIR / 'Audio'
        splits_dir = audio_dir / 'drums_split' / timestamp

        logging.info(f"🥁 Splitting drums: {drum_path}")
        drum_splits = engine.split_drums(drum_path, str(splits_dir))

        logging.info(f"✅ Drum split complete: {list(drum_splits.keys())}")
        return jsonify({
            'status': 'success',
            'splits': {
                name: f'/api/audio/drums_split/{timestamp}/{Path(path).name}'
                for name, path in drum_splits.items()
            }
        })

    except Exception as e:
        logging.error(f"Drum split error: {e}", exc_info=True)
        return jsonify({'error': f'Drum split failed: {str(e)}'}), 500


@app.route('/api/render-final-mix', methods=['POST'])
def render_final_mix():
    """Render final mixed WAV from stems with beatmatching, volumes, and beat offset"""
    data = request.json
    try:
        import shutil
        from pathlib import Path
        from mutagen.wave import WAVE
        from mutagen.id3 import TIT2, TPE1, TBPM, TKEY, COMM
        from mashup_engine import MashupEngine

        timestamps = data.get('timestamps')  # [timestamp_slot0, timestamp_slot1]
        # JSON object keys are always strings, so the request body has
        # {"0": {...}, "1": {...}} -- normalize to int keys once here so the
        # rest of this function can use `volumes.get(slot)` with slot as an
        # int (as it does everywhere else) without silently missing every
        # lookup.
        volumes = {int(k): v for k, v in (data.get('volumes') or {}).items()}
        crossfader = data.get('crossfader', 50)
        metadata_list = data.get('metadata', [None, None])
        beat_offsets = data.get('beat_offsets', [0, 0])  # beats to offset Song 2
        target_bpm = data.get('target_bpm')  # for beatmatching

        if not timestamps or not volumes:
            return jsonify({'error': 'Missing parameters'}), 400

        # Extract metadata from first loaded song
        meta = None
        for m in metadata_list:
            if m:
                meta = m
                break

        bpm_label = meta.get('bpm', '?') if meta else '?'
        key = meta.get('key', '?') if meta else '?'
        song_names = [m.get('filename', f'Song_{i+1}') if m else f'Song_{i+1}' for i, m in enumerate(metadata_list)]
        mix_name = '-'.join([Path(n).stem for n in song_names if n]) or 'mashup'

        # Create output directory
        output_dir = BASE_DIR / 'Audio' / 'renders'
        output_dir.mkdir(exist_ok=True)
        # Timestamp-prefixed so every render gets its own file/URL -- a
        # deterministic name here would mean re-rendering with different
        # settings (e.g. a new beat offset) overwrites the same filename,
        # risking a stale browser-cached copy being served for the "new"
        # download instead of the actual latest render.
        render_timestamp = int(time.time() * 1000)
        # WAV, not FLAC: FL Studio (and most other DAWs -- Logic, Cubase,
        # Reaper, Reason, Sound Forge, Samplitude) read tempo/key from a WAV
        # ACID chunk on import, not from FLAC Vorbis comments or ID3 tags --
        # those are correctly embedded but simply never checked by DAW
        # sample importers.
        final_wav = output_dir / f"{render_timestamp}_{mix_name}-{bpm_label}-{key}-mix.wav"

        # Collect stem files and metadata for engine.render()
        engine = MashupEngine()
        stems_list = [None, None]
        bpms = [None, None]
        beat_anchors = [None, None]

        for slot in range(2):
            if not timestamps[slot]:
                continue

            stems_dir = BASE_DIR / 'Audio' / 'stems' / timestamps[slot]

            # Get stem files (7-stem structure: vocals, kick, snare, hihat, tom, bass, other)
            stems = {}
            for stem_name in ['vocals', 'kick', 'snare', 'hihat', 'tom', 'bass', 'other']:
                stem_file = stems_dir / f"{stem_name}.wav"
                if stem_file.exists():
                    stems[stem_name] = str(stem_file)

            if stems:
                stems_list[slot] = stems

            # Extract BPM from metadata
            if metadata_list[slot]:
                bpm_str = str(metadata_list[slot].get('bpm', ''))
                # Parse BPM: can be "115", "115.5", "115-manual-115-measured"
                try:
                    bpms[slot] = float(bpm_str.split('-')[0])
                except (ValueError, IndexError):
                    bpms[slot] = None

                # From the original /api/process-song analysis (see the
                # frontend's metadataList for this endpoint) -- without this,
                # the beatmatch pre-pass in render() has no anchor to work
                # with and silently skips phase alignment entirely.
                beat_anchors[slot] = metadata_list[slot].get('beat_anchor')

        # Build params for engine.render()
        params = {
            'songs': stems_list,
            'stems': [s if s else {} for s in stems_list],
            'bpms': bpms,
            'beat_anchors': beat_anchors,
            'beat_offsets': beat_offsets,
            'target_bpm': target_bpm if target_bpm else None,
            'beatmatch': bool(target_bpm),  # Only beatmatch if target BPM is set
            'sliders': {
                's0_pitch_shift': 0.0,
                's0_speed': 1.0,
                's1_pitch_shift': 0.0,
                's1_speed': 1.0
            },
            # Raw 0-100 (matches render()'s own default of 50 and its
            # internal /100.0 -- pre-dividing here too used to silently
            # crush Song 2 to ~1% volume at the default 50/50 crossfader,
            # since render() would divide by 100 a SECOND time).
            'crossfader': crossfader
        }

        # Convert stem volumes to engine format -- these have to land inside
        # params['sliders'], since that's the only dict render()'s `sliders`
        # variable ever actually reads from.
        for slot in range(2):
            if volumes.get(slot):
                for stem_name, vol in volumes[slot].items():
                    params['sliders'][f's{slot}_{stem_name}_volume'] = vol

        logging.info(f"Rendering mix: beatmatch={params['beatmatch']}, beat_offsets={beat_offsets}, target_bpm={target_bpm}")

        # Render using engine (applies beatmatching and beat offset)
        try:
            engine.render(params, preview=False)
            output_file = BASE_DIR / 'final_remix.wav'

            if not output_file.exists():
                return jsonify({'error': 'Render output not found'}), 500

            shutil.move(str(output_file), str(final_wav))

            # Parse the bpm label the same way the rest of this endpoint
            # already does ("115", "115.5", "115-manual-115-measured").
            bpm_str = str(bpm_label).split('-')[0] if isinstance(bpm_label, str) else str(bpm_label)
            try:
                numeric_bpm = float(bpm_str)
            except (ValueError, TypeError):
                numeric_bpm = None

            if numeric_bpm:
                try:
                    engine.write_acid_chunk(str(final_wav), numeric_bpm, key if key in
                                             ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"] else None)
                    logging.info(f"✅ Wrote ACID chunk: {final_wav.name} (BPM: {numeric_bpm}, Key: {key})")
                except Exception as acid_err:
                    logging.error(f"ACID chunk write failed: {acid_err}", exc_info=True)
            else:
                logging.warning(f"No numeric BPM available ('{bpm_label}') -- skipping ACID chunk")

            # Basic descriptive tags too (title/artist/bpm/key/comment) --
            # secondary to the ACID chunk above, but still useful for any
            # player/tagger that reads ID3-in-WAV.
            try:
                audio = WAVE(str(final_wav))
                if audio.tags is None:
                    audio.add_tags()
                audio.tags.add(TIT2(encoding=3, text=f'{mix_name} Mix'))
                audio.tags.add(TPE1(encoding=3, text='DualSync Pro'))
                if numeric_bpm:
                    audio.tags.add(TBPM(encoding=3, text=str(int(round(numeric_bpm)))))
                audio.tags.add(TKEY(encoding=3, text=str(key)))
                audio.tags.add(COMM(encoding=3, lang='eng', desc='', text=f'Mixed with beatmatch (offset: {beat_offsets[1]} beats)'))
                audio.save()
            except Exception as tag_err:
                logging.error(f"WAV tagging failed: {tag_err}")

            logging.info(f"✅ Mix rendered with beatmatching: {final_wav}")
            return jsonify({
                'status': 'success',
                'file': f'/api/download-file/{final_wav.name}',
                'size_mb': round(final_wav.stat().st_size / (1024 * 1024), 2)
            })

        except Exception as render_err:
            logging.error(f"Engine render failed: {render_err}", exc_info=True)
            return jsonify({'error': f'Render failed: {str(render_err)}'}), 500

    except Exception as e:
        logging.error(f"Render error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/download-stems-zip', methods=['POST'])
def download_stems_zip():
    """Download all stems as a ZIP of WAVs, each carrying an ACID chunk
    (BPM/key) so DAWs can auto-detect tempo on import -- see _tag_stem_wav."""
    data = request.json
    try:
        import zipfile
        import io
        import tempfile
        from mashup_engine import MashupEngine

        timestamps = data.get('timestamps')
        metadata_list = data.get('metadata', [None, None])  # [{filename, bpm, key}, ...]
        include_original = data.get('include_original', True)
        include_processed = data.get('include_processed', True)

        engine = MashupEngine()
        temp_dir = tempfile.mkdtemp()

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for slot, timestamp in enumerate(timestamps):
                if not timestamp:
                    continue

                stems_dir = BASE_DIR / 'Audio' / 'stems' / timestamp
                meta = metadata_list[slot] or {}

                # Extract song name from filename (remove .mp3/.wav extension)
                raw_filename = meta.get('filename', f'Song_{slot + 1}')
                song_name = Path(raw_filename).stem

                bpm = meta.get('bpm', '?')
                key = meta.get('key', '?')

                logging.info(f"Processing slot {slot}: {song_name} ({key} {bpm}BPM)")

                # Dynamically discover available stems (supports both 7-stem legacy and 13-stem advanced)
                # Legacy priority: vocals, kick, snare, hihat, tom, bass, other
                # Advanced stems: vocals_lead, vocals_backing, kick, snare, hihat, tom, bass, guitar, piano, strings, synth_lead, synth_pad, ambient
                legacy_stems = ['vocals', 'kick', 'snare', 'hihat', 'tom', 'bass', 'other']
                advanced_stems = ['vocals_lead', 'vocals_backing', 'kick', 'snare', 'hihat', 'tom', 'bass', 'guitar', 'piano', 'strings', 'synth_lead', 'synth_pad', 'ambient']

                # Auto-detect which stems are available
                available_stems = []
                for stem in advanced_stems:
                    if (stems_dir / f"{stem}.wav").exists():
                        available_stems.append(stem)

                # Fall back to legacy if no advanced stems found
                if not available_stems:
                    available_stems = [s for s in legacy_stems if (stems_dir / f"{s}.wav").exists()]

                if include_original:
                    for stem in available_stems:
                        stem_file = stems_dir / f"{stem}.wav"
                        if stem_file.exists():
                            wav_name = f"{song_name}-{bpm}-{key}-{stem}.wav"
                            dest_path = Path(temp_dir) / wav_name
                            _tag_stem_wav(engine, stem_file, dest_path, song_name, bpm, key, stem)
                            zip_file.write(str(dest_path), f"original/{wav_name}")
                            logging.debug(f"  ✓ original/{wav_name}")

                if include_processed:
                    for stem in available_stems:
                        stem_file = stems_dir / f"{stem}_processed.wav"
                        if not stem_file.exists():
                            stem_file = stems_dir / f"{stem}.wav"

                        if stem_file.exists():
                            wav_name = f"{song_name}-{bpm}-{key}-{stem}.wav"
                            dest_path = Path(temp_dir) / f"processed_{wav_name}"
                            _tag_stem_wav(engine, stem_file, dest_path, song_name, bpm, key, stem)
                            zip_file.write(str(dest_path), f"processed/{wav_name}")
                            logging.debug(f"  ✓ processed/{wav_name}")

        # Cleanup temp directory
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

        zip_buffer.seek(0)
        timestamp_str = int(time.time() * 1000)
        zip_path = BASE_DIR / 'Audio' / f'stems_{timestamp_str}.zip'

        with open(zip_path, 'wb') as f:
            f.write(zip_buffer.getvalue())

        logging.info(f"✅ ZIP created: {zip_path}")
        return jsonify({
            'status': 'success',
            'file': f'/api/download-file/{zip_path.name}',
            'size_mb': round(zip_path.stat().st_size / (1024 * 1024), 2)
        })

    except Exception as e:
        logging.error(f"ZIP error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/download-unaligned-stems', methods=['POST'])
def download_unaligned_stems():
    """On-demand only: separate + download the stems of the ORIGINAL
    (pre-beatgrid-alignment) WAV for songs where 'Align beatgrid first' was
    used. Not run automatically -- Demucs separation is expensive and most
    users downloading the (already generated) aligned stems won't need it."""
    data = request.json or {}
    try:
        import zipfile
        import io
        import shutil
        import tempfile
        from mashup_engine import MashupEngine

        wav_filenames = data.get('wav_filenames', [None, None])
        metadata_list = data.get('metadata', [None, None])

        audio_dir = BASE_DIR / 'Audio'
        engine = MashupEngine()
        temp_dir = tempfile.mkdtemp()

        zip_buffer = io.BytesIO()
        wrote_any = False
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for slot, wav_filename in enumerate(wav_filenames):
                if not wav_filename:
                    continue
                wav_path = audio_dir / wav_filename
                if not wav_path.exists():
                    logging.error(f"Unaligned WAV not found for slot {slot}: {wav_filename}")
                    continue

                meta = metadata_list[slot] or {}
                raw_filename = meta.get('filename', f'Song_{slot + 1}')
                song_name = Path(raw_filename).stem
                bpm = meta.get('bpm', '?')
                key = meta.get('key', '?')

                logging.info(f"Separating unaligned original for slot {slot}: {song_name}")

                # Use multi-engine if enabled (same setting as main processing)
                import os
                use_multi_engine = os.getenv('DUALSYNC_MULTI_ENGINE', 'false').lower() == 'true'
                stem_dict = engine.separate_stems([str(wav_path)], use_multi_engine=use_multi_engine)[0]

                for stem_name, stem_path in stem_dict.items():
                    if not Path(stem_path).exists():
                        continue

                    wav_name = f"{song_name}-{bpm}-{key}-{stem_name}.wav"
                    dest_path = Path(temp_dir) / wav_name
                    _tag_stem_wav(engine, stem_path, dest_path, song_name, bpm, key, f"{stem_name} (unaligned original)")
                    zip_file.write(str(dest_path), f"unaligned_original/{wav_name}")
                    wrote_any = True
                    logging.debug(f"  ✓ {stem_name}")

        shutil.rmtree(temp_dir, ignore_errors=True)

        if not wrote_any:
            return jsonify({'error': 'No unaligned WAVs found to separate'}), 400

        zip_buffer.seek(0)
        timestamp_str = int(time.time() * 1000)
        zip_path = audio_dir / f'unaligned_stems_{timestamp_str}.zip'
        with open(zip_path, 'wb') as f:
            f.write(zip_buffer.getvalue())

        logging.info(f"✅ Unaligned stems ZIP created: {zip_path}")
        return jsonify({
            'status': 'success',
            'file': f'/api/download-file/{zip_path.name}',
            'size_mb': round(zip_path.stat().st_size / (1024 * 1024), 2)
        })

    except Exception as e:
        logging.error(f"Unaligned stems ZIP error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/download-file/<filename>')
def download_file(filename):
    """Download a file"""
    try:
        from pathlib import Path

        # Security: only allow files from specific directories
        safe_dirs = [
            BASE_DIR / 'Audio' / 'stems',
            BASE_DIR / 'Audio' / 'renders',
            BASE_DIR / 'Audio'
        ]

        file_path = None
        for safe_dir in safe_dirs:
            candidate = safe_dir / filename
            if candidate.exists() and candidate.is_file():
                file_path = candidate
                break

        if not file_path:
            return jsonify({'error': 'File not found'}), 404

        return send_file(str(file_path), as_attachment=True)

    except Exception as e:
        logging.error(f"Download error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/cleanup', methods=['POST'])
def cleanup_audio():
    """Clean up all generated audio files"""
    try:
        import shutil
        audio_dir = BASE_DIR / 'Audio'

        if audio_dir.exists():
            shutil.rmtree(str(audio_dir))
            audio_dir.mkdir(exist_ok=True)
            logging.info("✅ Cleaned up all audio files")
            return jsonify({'status': 'success', 'message': 'All audio files cleaned up'})
        else:
            return jsonify({'status': 'success', 'message': 'No audio files to clean'})
    except Exception as e:
        logging.error(f"Cleanup failed: {e}", exc_info=True)
        return jsonify({'error': f'Cleanup failed: {str(e)}'}), 500


@app.route('/api/process-status', methods=['GET'])
def process_status():
    """Get current processing status and progress"""
    global _processing_state
    return jsonify(_processing_state)


# ===== Health Check =====
@app.route('/api/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    # Werkzeug's built-in dev server (app.run) is documented as unfit for
    # production and, in practice here, would leave roughly half of the 14
    # concurrent streaming <audio> connections (7 stems x 2 songs) hung at
    # HAVE_METADATA forever -- never serviced, regardless of threaded=True.
    # Waitress is a real WSGI server with a proper connection/thread pool and
    # is pure-Python (works the same on Windows/Mac/Linux, no extra deps).
    from waitress import serve
    serve(app, host='127.0.0.1', port=5000, threads=32)
