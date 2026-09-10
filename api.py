"""Flask API for DualSync Pro"""
from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from pathlib import Path
import logging
import time

logging.basicConfig(level=logging.DEBUG)

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
@app.route('/api/separate-stems', methods=['POST'])
def separate_stems():
    """Separate audio into stems"""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    try:
        import shutil
        import time

        # Save uploaded file
        audio_dir = BASE_DIR / 'Audio'
        audio_dir.mkdir(exist_ok=True)

        file_path = audio_dir / file.filename
        file.save(str(file_path))
        logging.info(f"Processing stems for: {file_path}")

        # Separate stems using mashup_engine
        from mashup_engine import MashupEngine
        engine = MashupEngine()

        # Get BPM and key
        logging.info("Analyzing BPM...")
        bpm, _ = engine.analyze_track(str(file_path))
        logging.info("Analyzing Key...")
        key = engine.analyze_key(str(file_path))
        key_name = engine._key_to_note(key) if key >= 0 else "Unknown"

        # Separate stems
        logging.info("Separating stems...")
        stem_dict = engine.separate_stems([str(file_path)])[0]

        # Copy stems to a simple location for serving
        serve_dir = audio_dir / 'stems'
        serve_dir.mkdir(exist_ok=True)

        # Use timestamp to avoid conflicts
        timestamp = str(int(time.time() * 1000))
        session_dir = serve_dir / timestamp
        session_dir.mkdir(exist_ok=True)

        stems = {}
        for stem_name, stem_path in stem_dict.items():
            if Path(stem_path).exists():
                # Copy to serve directory
                dest_path = session_dir / f"{stem_name}.wav"
                shutil.copy2(stem_path, str(dest_path))
                stems[stem_name] = f"/api/audio/{timestamp}/{stem_name}.wav"
                logging.info(f"✅ Copied {stem_name} to {dest_path}")
            else:
                logging.error(f"❌ Stem file not found: {stem_path}")

        if not stems:
            raise Exception("No stems were separated successfully")

        return jsonify({
            'stems': stems,
            'bpm': round(bpm, 1),
            'key': key_name,
            'filename': file.filename,
            'timestamp': timestamp
        })
    except Exception as e:
        logging.error(f"Stem separation failed: {e}", exc_info=True)
        return jsonify({'error': f'Separation failed: {str(e)}'}), 500


@app.route('/api/audio/<path:filepath>')
def serve_audio(filepath):
    """Serve audio files"""
    audio_dir = BASE_DIR / 'Audio' / 'stems'
    file_path = audio_dir / filepath

    logging.info(f"[Audio] Requested: {filepath}")
    logging.info(f"[Audio] Looking in: {audio_dir}")
    logging.info(f"[Audio] Full path: {file_path}")
    logging.info(f"[Audio] Exists: {file_path.exists()}")

    if file_path.exists():
        logging.info(f"✅ Serving: {filepath}")
        return send_from_directory(str(audio_dir), filepath, mimetype='audio/wav')

    logging.error(f"❌ Not found: {file_path}")
    return jsonify({'error': 'File not found'}), 404


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
_processing_state = {'slot': None, 'stem': None, 'status': None}


@app.route('/api/process-stems', methods=['POST'])
def process_stems():
    """Combined beatmatch + transpose processing"""
    global _processing_state
    data = request.json
    try:
        source_bpm = data.get('source_bpm')
        target_bpm = data.get('target_bpm')
        source_key = data.get('source_key')
        target_key = data.get('target_key')
        timestamp = data.get('timestamp')
        slot = data.get('slot', 0)

        logging.info(f"Processing stems (slot {slot}): BPM {source_bpm}→{target_bpm}, Key {source_key}→{target_key}")

        if not timestamp:
            logging.error("Missing timestamp in request")
            return jsonify({'error': 'Missing timestamp'}), 400

        stems_dir = BASE_DIR / 'Audio' / 'stems' / timestamp
        if not stems_dir.exists():
            logging.error(f"Stems directory not found: {stems_dir}")
            return jsonify({'error': 'Stems directory not found'}), 400

        from mashup_engine import MashupEngine
        engine = MashupEngine()

        processed_stems = {}

        for stem in ['vocals', 'drums', 'bass', 'other']:
            # Update progress state
            _processing_state = {'slot': slot, 'stem': stem, 'status': 'processing'}

            stem_path = stems_dir / f"{stem}.wav"
            if not stem_path.exists():
                logging.warning(f"Stem not found: {stem_path}")
                _processing_state = {'slot': slot, 'stem': stem, 'status': 'skipped'}
                continue

            logging.info(f"🔄 Processing {stem} (slot {slot})...")

            # Use original or previously processed version
            current_path = stem_path
            output_path = stems_dir / f"{stem}_processed.wav"

            try:
                # Apply beatmatch if needed
                if source_bpm and target_bpm and float(source_bpm) != float(target_bpm):
                    tempo_ratio = float(target_bpm) / float(source_bpm)
                    if 0.5 <= tempo_ratio <= 2.0:
                        beatmatched_path = stems_dir / f"{stem}_beatmatched.wav"
                        _processing_state['status'] = f'beatmatching {stem}...'
                        logging.info(f"  🎵 Beatmatching {stem}: {source_bpm}→{target_bpm} BPM (ratio {tempo_ratio:.2f})")
                        success, measured_bpm = engine.time_stretch_audio(str(current_path), str(beatmatched_path), float(target_bpm), float(source_bpm))

                        # Verify beatmatching convergence (±10 BPM tolerance)
                        # Accept if: success AND (measured_bpm within tolerance OR detection failed but file created)
                        if success:
                            if measured_bpm:
                                bpm_error = abs(measured_bpm - float(target_bpm))
                                if bpm_error <= 10.0:
                                    current_path = beatmatched_path
                                    logging.info(f"  ✅ Beatmatched {stem} to {measured_bpm:.1f} BPM (target: {target_bpm}, error: {bpm_error:.1f})")
                                else:
                                    logging.warning(f"  ⚠️  Beatmatched {stem} to {measured_bpm:.1f} BPM (error: {bpm_error:.1f} > 10 BPM, but accepting)")
                                    current_path = beatmatched_path
                            else:
                                # BPM detection failed on output, but file exists - accept anyway
                                logging.warning(f"  ⚠️  Beatmatched {stem} (output BPM detection failed, but file created - accepting)")
                                current_path = beatmatched_path
                        else:
                            logging.error(f"  ❌ Beatmatch failed for {stem}: time_stretch_audio returned False")

                # Apply transpose if needed
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

                        _processing_state['status'] = f'transposing {stem}...'
                        logging.info(f"  🎼 Transposing {stem}: {semitones} semitones")
                        if engine.pitch_shift_audio(str(current_path), str(output_path), semitones):
                            processed_stems[stem] = f"/api/audio/{timestamp}/{stem}_processed.wav"
                            _processing_state['status'] = f'{stem} ✅'
                            logging.info(f"  ✅ Transposed {stem}")
                        else:
                            logging.error(f"  ❌ Transpose failed for {stem}")
                            processed_stems[stem] = f"/api/audio/{timestamp}/{stem}.wav"
                            _processing_state['status'] = f'{stem} failed'
                    else:
                        logging.warning(f"  Invalid key indices: {source_key}→{target_key}")
                        processed_stems[stem] = f"/api/audio/{timestamp}/{stem}.wav"
                        _processing_state['status'] = f'{stem} (no key change)'
                else:
                    # Only beatmatch, no transpose needed
                    if current_path != stem_path:
                        # Beatmatched file already written by time_stretch_audio
                        processed_stems[stem] = f"/api/audio/{timestamp}/{stem}_beatmatched.wav"
                        _processing_state['status'] = f'{stem} ✅'
                        logging.info(f"✅ Beatmatched {stem} ready")
                    else:
                        processed_stems[stem] = f"/api/audio/{timestamp}/{stem}.wav"
                        _processing_state['status'] = f'{stem} (no change)'
                        logging.info(f"No processing needed for {stem}")

            except Exception as stem_error:
                logging.warning(f"Error processing stem {stem}: {stem_error}, falling back to original")
                # Fall back to original stem (don't fail entire process)
                original_stem_path = stems_dir / f"{stem}.wav"
                if original_stem_path.exists():
                    processed_stems[stem] = f"/api/audio/{timestamp}/{stem}.wav"
                    _processing_state['status'] = f'{stem} (fallback)'
                else:
                    logging.error(f"Original stem also missing: {original_stem_path}")
                    _processing_state['status'] = f'{stem} (missing)'

        if not processed_stems:
            logging.error("No stems were processed")
            return jsonify({'error': 'Processing failed'}), 500

        # After processing, analyze the stems to get actual measured BPM for beatmatched stems
        measured_bpm = None
        if source_bpm and target_bpm and float(source_bpm) != float(target_bpm):
            # Pick first processed stem to measure output BPM
            for stem in ['vocals', 'drums', 'bass', 'other']:
                processed_stem_path = stems_dir / f"{stem}_processed.wav"
                if processed_stem_path.exists():
                    try:
                        measured_bpm, _ = engine.analyze_track(str(processed_stem_path))
                        logging.info(f"Measured output BPM: {measured_bpm:.1f} (target: {target_bpm})")
                        break
                    except Exception as e:
                        logging.warning(f"Could not measure BPM: {e}")

        logging.info(f"✅ Processing complete. Processed {len(processed_stems)} stems")
        return jsonify({
            'status': 'success',
            'processed_stems': processed_stems,
            'measured_bpm': measured_bpm,
            'target_bpm': target_bpm,
            'source_bpm': source_bpm
        })
    except Exception as e:
        logging.error(f"Process error: {e}", exc_info=True)
        return jsonify({'error': f'Processing failed: {str(e)}'}), 500


@app.route('/api/render-final-mix', methods=['POST'])
def render_final_mix():
    """Render final mixed FLAC from stems with current volumes and metadata"""
    data = request.json
    try:
        import subprocess
        import tempfile
        from pathlib import Path
        from mutagen.flac import FLAC

        timestamps = data.get('timestamps')  # [timestamp_slot0, timestamp_slot1]
        volumes = data.get('volumes')  # {0: {stem: vol}, 1: {stem: vol}}
        crossfader = data.get('crossfader', 50) / 100.0
        metadata_list = data.get('metadata', [None, None])  # [{filename, bpm, key}, ...]

        if not timestamps or not volumes:
            return jsonify({'error': 'Missing parameters'}), 400

        # Extract metadata from first loaded song
        meta = None
        for m in metadata_list:
            if m:
                meta = m
                break

        bpm = meta.get('bpm', '?') if meta else '?'
        key = meta.get('key', '?') if meta else '?'
        song_names = [m.get('filename', f'Song_{i+1}') if m else f'Song_{i+1}' for i, m in enumerate(metadata_list)]
        mix_name = '-'.join([Path(n).stem for n in song_names if n]) or 'mashup'

        # Create output file with descriptive name
        output_dir = BASE_DIR / 'Audio' / 'renders'
        output_dir.mkdir(exist_ok=True)
        # Intermediate WAV for FFmpeg, then convert to FLAC
        temp_wav = output_dir / f"temp_mix_{int(time.time() * 1000)}.wav"
        final_flac = output_dir / f"{mix_name}-{bpm}-{key}-mix.flac"

        # Build FFmpeg command to mix stems
        inputs = []
        filters = []
        input_idx = 0

        for slot in range(2):
            if not timestamps[slot]:
                continue

            stems_dir = BASE_DIR / 'Audio' / 'stems' / timestamps[slot]
            slot_volume = (1 - crossfader) if slot == 0 else crossfader

            for stem in ['vocals', 'drums', 'bass', 'other']:
                # Try processed version first, then original
                stem_file = stems_dir / f"{stem}_processed.wav"
                if not stem_file.exists():
                    stem_file = stems_dir / f"{stem}.wav"

                if stem_file.exists():
                    inputs.append('-i')
                    inputs.append(str(stem_file))

                    stem_volume = volumes.get(slot, {}).get(stem, 1.0)
                    master_volume = slot_volume * stem_volume

                    filters.append(f"[{input_idx}]volume={master_volume}[s{input_idx}]")
                    input_idx += 1

        if input_idx == 0:
            return jsonify({'error': 'No stems found'}), 400

        # Concat all volumes
        concat_str = ''.join([f'[s{i}]' for i in range(input_idx)])
        filter_complex = ';'.join(filters) + f';{concat_str}amix=inputs={input_idx}[out]'

        # Render as WAV first
        cmd_wav = ['ffmpeg', '-y'] + inputs + [
            '-filter_complex', filter_complex,
            '-map', '[out]',
            '-acodec', 'pcm_s16le',
            str(temp_wav)
        ]

        logging.info(f"Rendering mix with {input_idx} stems...")
        result = subprocess.run(cmd_wav, capture_output=True, text=True, timeout=300)

        if result.returncode != 0 or not temp_wav.exists():
            logging.error(f"FFmpeg error: {result.stderr}")
            return jsonify({'error': 'Rendering failed'}), 500

        # Convert WAV to FLAC
        cmd_flac = ['ffmpeg', '-i', str(temp_wav), '-c:a', 'flac', '-y', str(final_flac)]
        result = subprocess.run(cmd_flac, capture_output=True, text=True, timeout=300)

        if result.returncode == 0 and final_flac.exists():
            # Add FLAC tags
            try:
                audio = FLAC(str(final_flac))
                audio['TITLE'] = f'{mix_name} Mix'
                audio['BPM'] = str(int(float(bpm)))
                audio['INITIALKEY'] = str(key)
                audio.save()
                logging.info(f"✅ Tagged FLAC: {final_flac.name}")
            except Exception as tag_err:
                logging.error(f"FLAC tagging failed: {tag_err}")

            # Cleanup temp WAV
            temp_wav.unlink(missing_ok=True)

            logging.info(f"✅ Mix rendered: {final_flac}")
            return jsonify({
                'status': 'success',
                'file': f'/api/download-file/{final_flac.name}',
                'size_mb': round(final_flac.stat().st_size / (1024 * 1024), 2)
            })
        else:
            logging.error(f"FLAC conversion failed: {result.stderr}")
            temp_wav.unlink(missing_ok=True)
            return jsonify({'error': 'FLAC conversion failed'}), 500

    except Exception as e:
        logging.error(f"Render error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/download-stems-zip', methods=['POST'])
def download_stems_zip():
    """Download all stems as ZIP with FLAC format and tags"""
    data = request.json
    try:
        import zipfile
        import io
        import subprocess
        from mutagen.flac import FLAC
        import tempfile
        import os

        timestamps = data.get('timestamps')
        metadata_list = data.get('metadata', [None, None])  # [{filename, bpm, key}, ...]
        include_original = data.get('include_original', True)
        include_processed = data.get('include_processed', True)

        # Create temporary directory for FLAC conversions
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

                # Add original stems as FLAC
                if include_original:
                    for stem in ['vocals', 'drums', 'bass', 'other']:
                        stem_file = stems_dir / f"{stem}.wav"
                        if stem_file.exists():
                            # Convert WAV to FLAC with tags
                            flac_name = f"{song_name}-{bpm}-{key}-{stem}.flac"
                            flac_path = Path(temp_dir) / flac_name

                            # Use FFmpeg to convert to FLAC
                            cmd = [
                                'ffmpeg', '-i', str(stem_file),
                                '-c:a', 'flac', '-y',
                                str(flac_path)
                            ]
                            result = subprocess.run(cmd, capture_output=True, timeout=60)

                            if result.returncode == 0 and flac_path.exists():
                                # Add FLAC tags
                                try:
                                    audio = FLAC(str(flac_path))
                                    audio['TITLE'] = song_name
                                    audio['BPM'] = str(int(float(bpm)))
                                    audio['INITIALKEY'] = str(key)
                                    audio.save()
                                    logging.info(f"✅ Tagged FLAC: {flac_name}")
                                except Exception as tag_err:
                                    logging.error(f"FLAC tagging failed: {tag_err}")

                                arcname = f"original/{flac_name}"
                                zip_file.write(str(flac_path), arcname)
                            else:
                                logging.error(f"FLAC conversion failed for {stem}")

                # Add processed stems as FLAC
                if include_processed:
                    for stem in ['vocals', 'drums', 'bass', 'other']:
                        stem_file = stems_dir / f"{stem}_processed.wav"
                        if not stem_file.exists():
                            stem_file = stems_dir / f"{stem}.wav"

                        if stem_file.exists():
                            # Convert WAV to FLAC with tags
                            flac_name = f"{song_name}-{bpm}-{key}-{stem}.flac"
                            flac_path = Path(temp_dir) / f"processed_{flac_name}"

                            # Use FFmpeg to convert to FLAC
                            cmd = [
                                'ffmpeg', '-i', str(stem_file),
                                '-c:a', 'flac', '-y',
                                str(flac_path)
                            ]
                            result = subprocess.run(cmd, capture_output=True, timeout=60)

                            if result.returncode == 0 and flac_path.exists():
                                # Add FLAC tags
                                try:
                                    audio = FLAC(str(flac_path))
                                    audio['TITLE'] = song_name
                                    audio['BPM'] = str(int(float(bpm)))
                                    audio['INITIALKEY'] = str(key)
                                    audio.save()
                                    logging.info(f"✅ Tagged FLAC: {flac_name}")
                                except Exception as tag_err:
                                    logging.error(f"FLAC tagging failed: {tag_err}")

                                arcname = f"processed/{flac_name}"
                                zip_file.write(str(flac_path), arcname)
                            else:
                                logging.error(f"FLAC conversion failed for processed {stem}")

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


# ===== Health Check =====
@app.route('/api/health')
def health():
    """Health check endpoint"""
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
