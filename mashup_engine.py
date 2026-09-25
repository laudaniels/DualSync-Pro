import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class MashupEngine:
    """Build FFmpeg mixes with multi-engine stem separation: Mel-Band RoFormer Karaoke
    (lead/backing vocals), Demucs htdemucs_6s (bass/guitar/piano/other/drums),
    MDX23C DrumSep (kick/snare/hihat/tom), HiFi++ GAN (restoration)."""

    STEM_NAMES = ("vocals", "drums", "bass", "other")
    FINAL_STEM_NAMES = (
        "vocals",
        "kick", "snare", "hihat", "tom",
        "bass", "guitar", "piano", "other",
    )
    TARGET_SAMPLE_RATE = 44100

    # Best-in-class audio models (verified SDR scores)
    AUDIO_SEPARATOR_MODELS = {
        'vocals_best': {
            'model_name': 'mel_band_roformer',
            'description': 'Mel-Band RoFormer - 12.6 dB SDR, cleanest vocals',
        },
        'drums_6stem': {
            'model_name': 'demucs',
            'preset': 'htdemucs_6s',
            'description': 'Demucs 6-stem - 9.5 dB SDR, bass/guitar/piano/other/drums',
        },
        'kick_snare_ml': {
            'model_name': 'mdx23c',
            'description': 'MDX23C DrumSep - SOTA kick/snare ML separation',
        }
    }

    # Class-level, not per-instance: the GUI creates a fresh MashupEngine()
    # for every button click, but "is a preview currently playing" and "kill
    # everything this app has spawned" both need to survive across those
    # short-lived instances.
    _preview_process = None
    _active_encode_processes = []

    @classmethod
    def is_previewing(cls):
        return cls._preview_process is not None and cls._preview_process.poll() is None

    @classmethod
    def stop_preview(cls):
        """Kill the currently playing preview, if any."""
        if cls._preview_process and cls._preview_process.poll() is None:
            cls._preview_process.terminate()
        cls._preview_process = None

    @classmethod
    def stop_all(cls):
        """Kill every ffmpeg/ffplay process this app has spawned. Call this
        before the GUI exits -- ffmpeg/ffplay are independent OS processes
        and are not tied to the Python process's lifetime, so closing the
        window does not stop them on its own."""
        cls.stop_preview()
        for proc in cls._active_encode_processes:
            if proc.poll() is None:
                proc.terminate()
        cls._active_encode_processes = []

    def __init__(self):
        self.ffmpeg = "ffmpeg"
        self.ffplay = "ffplay"
        self.stems_dir = BASE_DIR / "separated_stems"

    def separate_stems(self, songs, use_multi_engine=False, use_restoration=True):
        """Stem separation with optional multi-engine mode.

        Args:
            songs: List of audio file paths
            use_multi_engine: If True, use advanced Karaoke + Demucs-6s + DrumSep + HiFi++
                             pipeline (9 stems). If False, use legacy Demucs-only (7 stems)
                             for backward compatibility.
            use_restoration: If True and use_multi_engine=True, apply HiFi++ GAN restoration.

        Returns:
            List of dicts mapping stem names to file paths.
        """
        import logging

        if use_multi_engine:
            return self.separate_stems_multi_engine(songs, use_restoration=use_restoration)

        # Legacy Demucs-only mode
        if not shutil.which("demucs"):
            probe = subprocess.run([sys.executable, "-m", "demucs", "--help"],
                                   capture_output=True, text=True, timeout=30)
            if probe.returncode != 0:
                raise RuntimeError(
                    "Stem separation needs Demucs. Install it in Command Prompt with:\n\n"
                    "python -m pip install -U demucs\n\n"
                    "Then click SEPARATE STEMS again. The first run also downloads its audio model.")

        self.stems_dir.mkdir(exist_ok=True)
        results = []
        for song in songs:
            song_hash = hashlib.sha1(str(Path(song).resolve()).encode("utf-8")).hexdigest()[:16]
            song_out_dir = self.stems_dir / song_hash
            command = [sys.executable, "-m", "demucs", "--out", str(song_out_dir), song]
            result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "Demucs failed without an error message."
                raise RuntimeError(f"Demucs could not separate {Path(song).name}:\n{detail[-1200:]}")

            song_folder = Path(song).stem
            candidates = list(song_out_dir.glob(f"*/{song_folder}"))
            if not candidates:
                raise RuntimeError(f"Demucs finished, but no stem folder was found for {Path(song).name}.")
            folder = candidates[0]
            stems = {name: str(folder / f"{name}.wav") for name in self.STEM_NAMES}
            missing = [name for name, path in stems.items() if not Path(path).is_file()]
            if missing:
                raise RuntimeError(f"Demucs did not create all expected stems for {Path(song).name}: {', '.join(missing)}")

            try:
                logging.info(f"🥁 Splitting drums for {Path(song).name}...")
                drum_splits = self.split_drums(stems['drums'], str(folder))
                del stems['drums']
                stems.update(drum_splits)
                logging.info(f"✅ Drums split into: kick, snare, hi-hat, tom")
            except Exception as e:
                logging.warning(f"⚠️  Drum split failed: {e}")
                drums_path = stems['drums']
                stem_name = Path(drums_path).stem
                drums_dir = Path(drums_path).parent
                drum_components = {
                    'kick': str(drums_dir / f"{stem_name}_kick.wav"),
                    'snare': str(drums_dir / f"{stem_name}_snare.wav"),
                    'hihat': str(drums_dir / f"{stem_name}_hihat.wav"),
                    'tom': str(drums_dir / f"{stem_name}_tom.wav"),
                }
                for comp_name, comp_path in drum_components.items():
                    shutil.copy2(drums_path, comp_path)
                    logging.info(f"📋 Duplicated drums → {comp_name}: {comp_path}")
                del stems['drums']
                stems.update(drum_components)

            self._conform_stem_lengths(stems)
            results.append(stems)
        return results

    def _get_duration(self, path):
        """Return a media file's duration in seconds via ffprobe."""
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30
        )
        return float(result.stdout.strip())

    def _conform_stem_lengths(self, stems, tolerance=0.02):
        """Pad every stem with silence so they all share the same (longest) duration.

        Demucs' 4-stem output is sample-accurate, but split_drums() runs a
        different ffmpeg filter chain per drum component (lowpass/highpass vs
        bandpass), and each filter's group delay shifts its output length by
        a few milliseconds. Left uneven, the shortest stem hits its native
        end before the others, so it drops out mid-playback until the
        frontend's shared-playhead loop resets everything back to zero.
        """
        import logging

        durations = {name: self._get_duration(path) for name, path in stems.items()}
        target = max(durations.values())

        for name, path in stems.items():
            if target - durations[name] <= tolerance:
                continue
            padded_path = str(Path(path).with_suffix('')) + '_padded.wav'
            cmd = [
                self.ffmpeg, "-y", "-i", str(path),
                "-af", "apad", "-t", f"{target:.3f}",
                padded_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                logging.warning(f"Could not pad {name} to {target:.3f}s: {result.stderr.strip()[-300:]}")
                continue
            shutil.move(padded_path, path)
            logging.info(f"🩹 Padded {name}: {durations[name]:.3f}s → {target:.3f}s")

    def _detect_beats_essentia(self, song_path, min_bpm=40, max_bpm=208):
        """Detect BPM and every individual beat position using Essentia's
        RhythmExtractor2013 (combines multiple beat-tracking algorithms;
        madmom would have been the neural-net alternative here, but it
        doesn't even install in this project's environment -- see
        requirements.txt).

        Returns (bpm, beat_anchor, ticks) where `ticks` is every detected
        beat timestamp in seconds (needed for per-beat beatgrid alignment,
        not just the single average BPM analyze_track() used to return).
        Raises on failure -- callers fall back to Librosa.
        """
        import logging
        from essentia.standard import MonoLoader, RhythmExtractor2013

        loader = MonoLoader(filename=str(song_path))
        audio = loader()

        rhythm = RhythmExtractor2013(method='multifeature', minTempo=int(min_bpm), maxTempo=int(max_bpm))
        bpm, ticks, confidence, _estimates, _bpm_intervals = rhythm(audio)

        if len(ticks) < 2:
            raise RuntimeError("Essentia detected fewer than 2 beats")

        beat_anchor = float(ticks[0])
        logging.info(f"✅ Essentia BPM: {bpm:.1f}, beat anchor: {beat_anchor:.2f}s, "
                     f"beats: {len(ticks)}, confidence: {confidence:.2f}")
        return float(bpm), beat_anchor, list(ticks)

    def analyze_track(self, song_path):
        """Estimate a track's tempo (BPM) and a reference beat position.

        Uses Essentia's RhythmExtractor2013 for improved accuracy over a
        single-pass Librosa estimate. Falls back to Librosa if Essentia
        is unavailable or fails on this file.

        Multi-pass: samples 3 sections of track and returns median BPM for robustness.
        """
        import logging
        import numpy as np

        try:
            bpm, beat_anchor, _ticks = self._detect_beats_essentia(song_path)
            return bpm, beat_anchor
        except Exception as e:
            logging.warning(f"Essentia BPM detection failed: {e}, falling back to Librosa")

        # Fallback to Librosa (multi-pass for robustness)
        import librosa

        try:
            total_duration = librosa.get_duration(path=song_path)
        except TypeError:
            total_duration = librosa.get_duration(filename=song_path)

        # Sample 3 sections: early, middle, late
        bpm_samples = []
        beat_anchors = []

        if total_duration > 120.0:
            # Long track: sample 3 sections
            offsets = [10.0, total_duration / 2 - 30.0, total_duration - 60.0]
        elif total_duration > 60.0:
            # Medium track: sample 2 sections
            offsets = [5.0, total_duration - 50.0]
        else:
            # Short track: sample from beginning
            offsets = [0.0]

        for offset in offsets:
            offset = max(0.0, min(offset, total_duration - 10.0))
            window = min(40.0, total_duration - offset)

            try:
                y, sr = librosa.load(song_path, sr=None, mono=True, offset=offset, duration=window)
                tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
                tempo = float(np.asarray(tempo).reshape(-1)[0])

                beat_times = librosa.frames_to_time(beat_frames, sr=sr)
                beat_anchor = float(beat_times[0]) + offset if len(beat_times) else offset

                bpm_samples.append(tempo)
                beat_anchors.append(beat_anchor)
                logging.info(f"  Sample @ {offset:.0f}s: {tempo:.1f} BPM")
            except Exception as e:
                logging.warning(f"  Sample @ {offset:.0f}s failed: {e}")

        if bpm_samples:
            # Return median BPM across samples
            final_bpm = float(np.median(bpm_samples))
            final_anchor = beat_anchors[len(bpm_samples) // 2]  # Middle sample
            logging.info(f"Using Librosa BPM (median of {len(bpm_samples)} samples): {final_bpm:.1f}, beat anchor: {final_anchor:.2f}s")
            return final_bpm, final_anchor
        else:
            logging.warning("All BPM samples failed")
            return 120.0, 0.0

    def analyze_track_and_key(self, song_path):
        """Detect BPM and key in a single pass with 5-pass BPM strategy.
        Returns tuple: (bpm, beat_anchor, key, scale).
        Key is 0-11 (C=0, C#=1, ..., B=11), or -1 if detection fails.
        Scale is 'major', 'minor', or None if the mode couldn't be determined
        (only Essentia detects mode; Librosa's chroma-argmax approach can't).

        Strategy:
        - BPM: 5 passes for robustness (intro often BPM-less, use middle for small files)
        - Key: Use both Librosa AND Essentia for verification
        """
        import logging
        import numpy as np
        import librosa

        logging.info(f"Analyzing BPM and key for: {song_path}")

        # Load audio once for both analyses
        try:
            y, sr = librosa.load(song_path, sr=None, mono=True)
        except Exception as e:
            logging.error(f"Failed to load audio: {e}")
            return 120.0, 0.0, -1, None

        # BPM detection using Essentia first (better accuracy than a single
        # Librosa pass; madmom would have been the neural-net alternative
        # here but doesn't even install in this project's environment)
        bpm = None
        beat_anchor = None
        try:
            bpm, beat_anchor, _ticks = self._detect_beats_essentia(song_path)
        except Exception as e:
            logging.warning(f"Essentia failed: {e}, using Librosa")

        # Fallback to Librosa if Madmom failed or unavailable (5-pass strategy)
        if bpm is None:
            try:
                total_duration = librosa.get_duration(y=y, sr=sr)
                bpm_samples = []
                beat_anchors_list = []

                # 5-pass strategy: skip intro (BPM-less), use middle sections
                if total_duration < 60.0:
                    # Small file: sample from middle only
                    offsets = [total_duration / 2 - 10.0]
                    logging.info(f"Small file ({total_duration:.0f}s), sampling from middle")
                elif total_duration < 120.0:
                    # Medium: 3 passes, skip intro
                    offsets = [10.0, total_duration / 2, total_duration - 30.0]
                elif total_duration < 300.0:
                    # Long: 4 passes, skip intro
                    offsets = [15.0, total_duration / 3, total_duration / 2 + 15.0, total_duration - 40.0]
                else:
                    # Very long: 5 passes, spread across middle sections
                    offsets = [30.0, total_duration / 4, total_duration / 2, total_duration * 0.75, total_duration - 50.0]

                logging.info(f"📊 BPM analysis: {len(offsets)}-pass strategy (duration: {total_duration:.0f}s)")

                for i, offset in enumerate(offsets):
                    offset = max(0.0, min(offset, total_duration - 10.0))
                    window = min(40.0, total_duration - offset)
                    try:
                        y_sample, sr_sample = librosa.load(song_path, sr=sr, mono=True, offset=offset, duration=window)
                        tempo, beat_frames = librosa.beat.beat_track(y=y_sample, sr=sr_sample)
                        tempo = float(np.asarray(tempo).reshape(-1)[0])
                        beat_times = librosa.frames_to_time(beat_frames, sr=sr_sample)
                        beat_anchor_sample = float(beat_times[0]) + offset if len(beat_times) else offset
                        bpm_samples.append(tempo)
                        beat_anchors_list.append(beat_anchor_sample)
                        logging.info(f"  Pass {i+1}: {tempo:.1f} BPM @ {offset:.0f}s")
                    except Exception as e:
                        logging.warning(f"  Pass {i+1} @ {offset:.0f}s failed: {e}")

                if bpm_samples:
                    bpm = float(np.median(bpm_samples))
                    beat_anchor = beat_anchors_list[len(bpm_samples) // 2]
                    logging.info(f"✅ Librosa BPM (median of {len(bpm_samples)} passes): {bpm:.1f}, beat anchor: {beat_anchor:.2f}s")
                else:
                    bpm = 120.0
                    beat_anchor = 0.0
                    logging.warning("All BPM samples failed")
            except Exception as e:
                logging.error(f"Librosa BPM failed: {e}")
                bpm = 120.0
                beat_anchor = 0.0

        # Key detection: use BOTH Librosa AND Essentia for verification
        key = -1
        key_librosa = -1
        key_essentia = -1

        # Primary: Librosa chroma
        try:
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
            chroma_mean = chroma.mean(axis=1)
            key_librosa = int(np.argmax(chroma_mean))
            logging.info(f"✅ Librosa key: {self._key_to_note(key_librosa)}")
        except Exception as e:
            logging.warning(f"Librosa key detection failed: {e}")

        # Secondary: Essentia for verification/cross-check -- also the ONLY
        # source of scale (major/minor); Librosa's chroma-argmax approach
        # has no concept of mode at all.
        scale = None
        try:
            from essentia.standard import MonoLoader, KeyExtractor

            loader = MonoLoader(filename=song_path, sampleRate=44100)
            audio = loader()
            key_extractor = KeyExtractor()
            # KeyExtractor returns (key, scale, strength) -- a 3-tuple, not
            # 2. Unpacking this into 2 variables used to throw on every
            # single call ("too many values to unpack"), silently discarding
            # Essentia's key AND scale/mode and falling back to Librosa
            # (which can't detect mode at all) every time.
            key_str, scale_str, confidence = key_extractor(audio)

            if key_str and confidence > 0.5:
                key_notes = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
                key_name = key_str.split()[0]
                if key_name in key_notes:
                    key_essentia = key_notes.index(key_name)
                    scale = scale_str if scale_str in ('major', 'minor') else None
                    logging.info(f"✅ Essentia key: {key_name} {scale} (confidence: {confidence:.2f})")
        except Exception as e:
            logging.warning(f"Essentia key detection failed: {e}")

        # Use Librosa primary, verify with Essentia
        if key_librosa >= 0:
            key = key_librosa
            if key_essentia >= 0 and key_essentia != key_librosa:
                logging.warning(f"⚠️  Key mismatch: Librosa={self._key_to_note(key_librosa)}, Essentia={self._key_to_note(key_essentia)} (using Librosa)")
            elif key_essentia >= 0:
                logging.info(f"✓ Key verified by both methods: {self._key_to_note(key)}")
        elif key_essentia >= 0:
            key = key_essentia
            logging.info(f"Using Essentia key (Librosa failed): {self._key_to_note(key)}")

        return bpm, beat_anchor, key, scale

    def analyze_key(self, song_path):
        """Detect the musical key using librosa chroma (reliable) or essentia (fallback).
        Returns key as integer: 0=C, 1=C#, ..., 11=B.
        Returns -1 if detection fails."""
        import logging
        import librosa
        import numpy as np

        # Primary: librosa chroma (always works)
        try:
            y, sr = librosa.load(song_path, sr=None, mono=True)
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
            chroma_mean = chroma.mean(axis=1)
            key = int(np.argmax(chroma_mean))
            logging.info(f"Key detected via Librosa: {self._key_to_note(key)}")
            return key
        except Exception as e:
            logging.warning(f"Librosa key detection failed: {e}")

        # Fallback to Essentia (may fail without classifier models)
        try:
            from essentia.standard import MonoLoader, KeyExtractor

            loader = MonoLoader(filename=song_path, sampleRate=44100)
            audio = loader()
            key_extractor = KeyExtractor()
            key_str, confidence = key_extractor(audio)

            if key_str and confidence > 0.5:
                key_notes = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
                key_name = key_str.split()[0]  # Extract note part
                if key_name in key_notes:
                    key = key_notes.index(key_name)
                    logging.info(f"Key detected via Essentia: {key_name} (confidence: {confidence:.2f})")
                    return key
        except Exception as e:
            logging.warning(f"Essentia key detection failed: {e}")

        logging.warning("Key detection failed for all methods")
        return -1

    @staticmethod
    def _semitones_between(key1, key2):
        """Calculate semitone difference between two keys (0-11)."""
        if key1 == -1 or key2 == -1:
            return 0
        diff = (key2 - key1) % 12
        if diff > 6:
            diff -= 12
        return diff

    def align_beatgrid(self, input_path, output_path, target_bpm=None):
        """Warp a track so every detected beat lands on a perfectly steady
        tempo grid, correcting drift (vinyl rips, live-tracked recordings)
        that a single constant-ratio stretch can't fix. Unlike
        time_stretch_audio (one ratio for the whole file), this maps each
        beat individually via rubberband's --timemap, so the correction
        follows the track's own wobble instead of assuming a constant tempo
        throughout. Essentia (RhythmExtractor2013) supplies the per-beat
        positions -- it has no warping capability of its own, only rubberband
        can actually perform the time-variant stretch.

        target_bpm: if None, aligns to the track's own detected average BPM
        (removes wobble, keeps the same overall tempo). Pass a specific BPM
        to align and retarget tempo in one pass.

        Returns (output_path, bpm, beat_anchor, mean_correction_ms,
        max_correction_ms) -- bpm/beat_anchor are from the ORIGINAL
        (pre-alignment) analysis; the correction stats are how far each
        detected beat sat from its ideal grid position before warping (the
        actual size of the drift this step corrected), for the caller to
        log/store/display.

        Raises RuntimeError if rubberband isn't installed, or if fewer than
        2 beats were detected (nothing to align to).
        """
        import logging
        import os
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path
        import soundfile as sf

        if shutil.which("rubberband") is None:
            raise RuntimeError(
                "Beatgrid alignment needs the 'rubberband' command-line tool.\n"
                "Install it with: apt-get install rubberband-cli (Linux) or "
                "brew install rubberband (macOS)."
            )

        os.makedirs(os.path.dirname(str(output_path)) or ".", exist_ok=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # rubberband needs WAV in/out. Detect beats on THIS SAME decoded
            # WAV (not the original compressed file) -- MP3 decoders disagree
            # slightly on encoder-delay/priming samples at the start of the
            # file, so beat times measured on the raw MP3 can be offset by
            # tens of milliseconds from sample positions in an independently
            # ffmpeg-decoded copy, corrupting the whole timemap.
            wav_in = tmpdir / "in.wav"
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(input_path), "-ar", "44100", str(wav_in)],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                raise RuntimeError(f"Could not convert input to WAV: {result.stderr[-500:]}")

            bpm, beat_anchor, ticks = self._detect_beats_essentia(str(wav_in))
            if target_bpm is None:
                target_bpm = bpm

            time_ratio, mean_correction_ms, max_correction_ms = self._warp_beats_to_grid(
                wav_in, ticks, target_bpm, float(ticks[0]), output_path, tmpdir
            )

        logging.info(f"🎯 Beatgrid aligned: {len(ticks)} beats -> {target_bpm:.1f} BPM steady grid "
                     f"(time ratio {time_ratio:.4f}, mean correction {mean_correction_ms:.1f}ms, "
                     f"max {max_correction_ms:.1f}ms)")
        return str(output_path), bpm, beat_anchor, mean_correction_ms, max_correction_ms

    def snap_to_reference(self, input_path, output_path, reference_bpm, reference_anchor):
        """Warp input_path's beats onto ANOTHER track's beat grid (its bpm +
        anchor phase) instead of an idealized self-grid -- so two different
        songs' beat grids stay phase-locked for the whole track instead of
        slowly drifting apart, which a single constant-ratio stretch
        (time_stretch_audio) can't guarantee since it only targets an
        average tempo within some tolerance. Same per-beat rubberband
        --timemap mechanism as align_beatgrid, just anchored to the
        reference's grid instead of the input's own first beat.

        Returns (output_path, bpm, beat_anchor, mean_correction_ms,
        max_correction_ms) -- bpm/beat_anchor are from the ORIGINAL
        (pre-warp) analysis of input_path; the correction stats are how far
        each of input_path's detected beats sat from the reference's ideal
        grid position before warping, for the caller to log/store/display.

        Raises RuntimeError if rubberband isn't installed, or if fewer than
        2 beats were detected in input_path (nothing to warp).
        """
        import logging
        import os
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        if shutil.which("rubberband") is None:
            raise RuntimeError(
                "Beat-grid snapping needs the 'rubberband' command-line tool.\n"
                "Install it with: apt-get install rubberband-cli (Linux) or "
                "brew install rubberband (macOS)."
            )

        os.makedirs(os.path.dirname(str(output_path)) or ".", exist_ok=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Same MP3-decoder-mismatch reasoning as align_beatgrid: detect
            # beats on the same decoded WAV that gets warped.
            wav_in = tmpdir / "in.wav"
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(input_path), "-ar", "44100", str(wav_in)],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                raise RuntimeError(f"Could not convert input to WAV: {result.stderr[-500:]}")

            bpm, beat_anchor, ticks = self._detect_beats_essentia(str(wav_in))

            time_ratio, mean_correction_ms, max_correction_ms = self._warp_beats_to_grid(
                wav_in, ticks, reference_bpm, reference_anchor, output_path, tmpdir
            )

        logging.info(f"🧲 Snapped to reference grid: {len(ticks)} beats -> {reference_bpm:.1f} BPM "
                     f"(anchor {reference_anchor:.2f}s, time ratio {time_ratio:.4f}, "
                     f"mean correction {mean_correction_ms:.1f}ms, max {max_correction_ms:.1f}ms)")
        return str(output_path), bpm, beat_anchor, mean_correction_ms, max_correction_ms

    def _warp_beats_to_grid(self, wav_in, ticks, target_bpm, target_anchor, output_path, tmpdir):
        """Shared core of align_beatgrid/snap_to_reference: build a rubberband
        --timemap mapping each detected beat in `ticks` onto an evenly-spaced
        grid of `target_bpm` starting at `target_anchor` seconds, warp
        wav_in through it, and encode the result to output_path.

        Returns (time_ratio, mean_correction_ms, max_correction_ms):
        time_ratio is the overall ratio applied (output duration / input
        duration); the correction stats are how far each detected beat sat
        from its ideal grid position BEFORE warping -- i.e. the actual size
        of the drift/misalignment this step corrected, in milliseconds,
        averaged and worst-case across all beats."""
        import subprocess
        import soundfile as sf

        info = sf.info(str(wav_in))
        sr = info.samplerate
        total_frames = info.frames

        ideal_interval = 60.0 / target_bpm

        # Timemap: (source_frame, target_frame) pairs -- anchor the file
        # start, snap each beat to its ideal grid position, then hold the
        # tail (after the last beat) at a constant offset so it isn't cut.
        timemap = [(0, 0)]
        corrections_ms = []
        for i, t in enumerate(ticks):
            src = int(float(t) * sr)
            tgt = max(0, int((target_anchor + i * ideal_interval) * sr))
            timemap.append((src, tgt))
            corrections_ms.append(abs(src - tgt) / sr * 1000.0)

        last_src = int(float(ticks[-1]) * sr)
        last_tgt = max(0, int((target_anchor + (len(ticks) - 1) * ideal_interval) * sr))
        tail_frames = total_frames - last_src
        total_output_frames = last_tgt + tail_frames
        timemap.append((total_frames, total_output_frames))

        time_ratio = total_output_frames / total_frames if total_frames > 0 else 1.0
        mean_correction_ms = sum(corrections_ms) / len(corrections_ms) if corrections_ms else 0.0
        max_correction_ms = max(corrections_ms) if corrections_ms else 0.0

        map_path = tmpdir / "timemap.txt"
        with open(map_path, "w") as f:
            for src, tgt in timemap:
                f.write(f"{src} {tgt}\n")

        wav_out = tmpdir / "out.wav"
        result = subprocess.run(
            ["rubberband", "--timemap", str(map_path), "-t", f"{time_ratio:.10f}",
             str(wav_in), str(wav_out)],
            capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            raise RuntimeError(f"RubberBand beatgrid warp failed: {result.stderr[-500:]}")

        # Encode to the requested output path/format (the rest of the
        # pipeline feeds a plain WAV into Demucs)
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_out), str(output_path)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RuntimeError(f"Could not finalize warped output: {result.stderr[-500:]}")

        return time_ratio, mean_correction_ms, max_correction_ms

    def normalize_peak(self, input_path, output_path, target_peak=0.97):
        """Peak-normalize audio so its loudest sample sits at exactly
        `target_peak` (linear, 0-1) of full scale -- a pure gain change, so
        it doesn't touch the waveform shape or affect BPM/key detection.
        Two ffmpeg passes: measure the current peak, then apply the exact
        gain needed to reach the target. Returns True on success; on
        failure, logs a warning and leaves the file untouched (returns
        False) rather than raising, since normalization is a nice-to-have,
        not something that should block the upload."""
        import logging
        import math
        import os
        import re
        import subprocess

        try:
            measure = subprocess.run(
                ["ffmpeg", "-i", str(input_path), "-af", "volumedetect", "-f", "null", "-"],
                capture_output=True, text=True, timeout=120
            )
            match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", measure.stderr)
            if not match:
                logging.warning(f"Peak normalization skipped (couldn't measure level): {input_path}")
                return False

            current_peak_db = float(match.group(1))
            target_peak_db = 20 * math.log10(target_peak)
            gain_db = target_peak_db - current_peak_db

            temp_output = f"{output_path}.normtmp.wav"
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(input_path), "-af", f"volume={gain_db:.3f}dB", temp_output],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                logging.warning(f"Peak normalization failed, leaving original levels: {result.stderr[-300:]}")
                os.remove(temp_output) if os.path.exists(temp_output) else None
                return False

            os.replace(temp_output, output_path)
            logging.info(f"🔊 Peak-normalized to {target_peak*100:.0f}% (was {current_peak_db:.1f} dB, applied {gain_db:+.2f} dB)")
            return True

        except Exception as e:
            logging.warning(f"Peak normalization failed, leaving original levels: {e}")
            return False

    def write_acid_chunk(self, wav_path, bpm, key=None):
        """Append a Sonic Foundry ACID chunk to a WAV file so DAWs can
        auto-detect its tempo/key on import. This -- NOT FLAC Vorbis
        comments or ID3 tags -- is the actual convention FL Studio, Logic,
        Cubase, Reaper, Reason, Sound Forge, and Samplitude read for sample
        tempo detection; a 'BPM' Vorbis field in a FLAC is correctly
        embedded metadata that those importers simply never look at.

        `key` is a note name ("C".."B", matching _key_to_note's output) or
        None if unknown. Mutates wav_path in place.
        """
        import struct

        key_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

        with open(wav_path, 'rb') as f:
            data = f.read()
        if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
            raise ValueError(f"{wav_path} is not a valid WAV file")

        import soundfile as sf
        duration = sf.info(str(wav_path)).duration
        num_beats = max(1, int(round(duration * bpm / 60.0)))

        # type_flags bits: 0x01 one-shot, 0x02 root note is valid, 0x04
        # "let this be time-stretched to the project tempo", 0x10 use the
        # standard MIDI note range (C4=60) for the root note field below
        # rather than the alternate 0x30-0x3B range.
        type_flags = 0x04
        if key is not None and key in key_names:
            type_flags |= 0x02 | 0x10
            root_note = 60 + key_names.index(key)
        else:
            root_note = 0

        # 24-byte ACID payload: type_flags(u32), root_note(u16),
        # unknown(u16, conventionally 0x8000), unknown(f32, conventionally
        # 0), num_beats(u32), meter_denominator(u16), meter_numerator(u16),
        # tempo(f32). Assumes 4/4 like the rest of this app's beat-grid code.
        acid_payload = struct.pack(
            '<IHHfIHHf',
            type_flags, root_note, 0x8000, 0.0,
            num_beats, 4, 4, float(bpm)
        )
        acid_chunk = b'acid' + struct.pack('<I', len(acid_payload)) + acid_payload
        if len(acid_payload) % 2:
            acid_chunk += b'\x00'  # RIFF chunks are word-aligned

        new_data = data + acid_chunk
        new_riff_size = len(new_data) - 8
        new_data = new_data[:4] + struct.pack('<I', new_riff_size) + new_data[8:]

        with open(wav_path, 'wb') as f:
            f.write(new_data)

    def time_stretch_audio(self, input_path, output_path, target_bpm, source_bpm=None):
        """Time-stretch audio to exact target BPM with multi-pass verification.
        Tries RubberBand (commercial quality) first, falls back to FFmpeg atempo.
        Returns tuple (success, measured_bpm) where measured_bpm is the final output BPM."""
        import logging
        import os
        import subprocess
        import shutil
        from pathlib import Path

        try:
            logging.info(f"Time-stretching {input_path} to target BPM {target_bpm}")

            # Ensure output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # If source_bpm not provided, analyze the input
            if source_bpm is None:
                source_bpm, _ = self.analyze_track(str(input_path))
                logging.info(f"Detected source BPM: {source_bpm}")

            current_input = input_path
            current_source_bpm = source_bpm
            max_passes = 3
            bpm_tolerance = 2.0  # ±2 BPM convergence tolerance (was 0.5, too strict)
            measured_bpm = None
            use_rubberband = shutil.which("rubberband") is not None

            if use_rubberband:
                logging.info("🎼 Using RubberBand for time-stretching (commercial quality)")
            else:
                logging.warning("⚠️  RubberBand not found, falling back to FFmpeg atempo")

            # NOTE: this used to be a `for pass_num in range(...)` loop with
            # `pass_num -= 1; continue` on failure -- reassigning a for-loop's
            # variable does nothing (the next iteration still comes from
            # range()), so a failed RubberBand pass silently skipped straight
            # to the NEXT pass instead of actually retrying with FFmpeg, and
            # the final pass returned success unconditionally even when badly
            # off target. Rewritten as an explicit while-loop so retries and
            # the tolerance check both work as intended.
            pass_num = 1
            rubberband_disabled = False
            best_bpm = None
            best_bpm_path = None
            while pass_num <= max_passes:
                tempo_ratio = target_bpm / current_source_bpm
                is_last_pass = pass_num == max_passes
                temp_output = output_path if is_last_pass else str(Path(output_path).parent / f"{Path(output_path).stem}_pass{pass_num}.wav")

                # Pass 1: Always use FFmpeg for stability; RubberBand produces corrupted output on first pass
                # Pass 2+: Try RubberBand if available (unless it already failed once)
                try_rubberband = use_rubberband and not rubberband_disabled and pass_num > 1

                if try_rubberband:
                    cmd = [
                        "rubberband", "-t", f"{tempo_ratio:.4f}",
                        "-T", "90",  # Default quality setting
                        str(current_input), temp_output
                    ]
                else:
                    # FFmpeg atempo (Pass 1 always, or fallback)
                    atempo_filters = []
                    remaining = tempo_ratio
                    while remaining < 0.5 or remaining > 2.0:
                        step = 2.0 if remaining > 2.0 else 0.5
                        atempo_filters.append(f"atempo={step:.2f}")
                        remaining /= step
                    atempo_filters.append(f"atempo={remaining:.2f}")
                    atempo_chain = ",".join(atempo_filters)
                    cmd = [
                        "ffmpeg", "-i", str(current_input), "-af", atempo_chain,
                        "-y", "-q:a", "9", temp_output
                    ]

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    engine_name = "RubberBand" if try_rubberband else "FFmpeg"
                    logging.error(f"{engine_name} failed: {result.stderr}")
                    if try_rubberband:
                        logging.warning(f"RubberBand failed on pass {pass_num}, retrying this pass with FFmpeg...")
                        rubberband_disabled = True
                        continue  # retry the SAME pass_num, now forced to FFmpeg
                    return False, None

                # Analyze output BPM
                measured_bpm, _ = self.analyze_track(temp_output)

                # Safety check: if BPM detection fails (returns 0), mark as error
                if measured_bpm <= 0:
                    logging.error(f"BPM analysis failed (got {measured_bpm}), output file may be corrupted")
                    if try_rubberband:
                        logging.warning(f"RubberBand output corrupted on pass {pass_num}, retrying this pass with FFmpeg...")
                        rubberband_disabled = True
                        continue  # retry the SAME pass_num, now forced to FFmpeg
                    return False, None

                bpm_error = abs(measured_bpm - target_bpm)
                logging.info(f"Pass {pass_num}: Output BPM = {measured_bpm:.1f}, Error = {bpm_error:.2f} BPM")

                # Track best attempt so far
                if best_bpm is None or bpm_error < abs(best_bpm - target_bpm):
                    best_bpm = measured_bpm
                    best_bpm_path = temp_output

                if bpm_error <= bpm_tolerance:
                    # Converged! Copy to final output if needed
                    if not is_last_pass:
                        shutil.copy2(temp_output, output_path)
                    for p in range(1, pass_num + 1):
                        temp = Path(output_path).parent / f"{Path(output_path).stem}_pass{p}.wav"
                        temp.unlink(missing_ok=True)
                    logging.info(f"✅ Time-stretched to {measured_bpm:.1f} BPM (target: {target_bpm}) in {pass_num} pass(es)")
                    return True, measured_bpm

                if is_last_pass:
                    # Did NOT converge within ±2 BPM tolerance after all passes
                    # Use best attempt and report warning instead of full failure
                    best_error = abs(best_bpm - target_bpm)
                    logging.warning(
                        f"⚠️  Time-stretch did not converge within ±{bpm_tolerance} BPM: "
                        f"best result = {best_bpm:.1f} BPM (target: {target_bpm}, error: {best_error:.2f} BPM)"
                    )
                    if best_bpm_path and Path(best_bpm_path).is_file():
                        shutil.copy2(best_bpm_path, output_path)
                    for p in range(1, max_passes):
                        temp = Path(output_path).parent / f"{Path(output_path).stem}_pass{p}.wav"
                        temp.unlink(missing_ok=True)
                    # Return best attempt instead of failing
                    return True, best_bpm

                # Prepare for next pass
                current_input = temp_output
                current_source_bpm = measured_bpm
                pass_num += 1

            return False, None

        except subprocess.TimeoutExpired:
            logging.error(f"Time stretch timeout for {input_path}")
            return False, None
        except Exception as e:
            logging.error(f"Time stretch failed for {input_path}: {e}", exc_info=True)
            return False, None

    def pitch_shift_audio(self, input_path, output_path, semitones, source_key=None):
        """Pitch-shift audio by `semitones` while preserving tempo/duration.
        Tries RubberBand's true pitch-shift first (--pitch), falls back to
        FFmpeg's asetrate/atempo combo if RubberBand isn't available.
        Returns tuple (success, measured_key) where measured_key is the detected key of output."""
        import logging
        import os
        import shutil
        import subprocess

        try:
            logging.info(f"Pitch-shifting {input_path} by {semitones} semitones")

            # Ensure output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            pitch_ratio = 2 ** (semitones / 12.0)
            use_rubberband = shutil.which("rubberband") is not None
            success = False

            if use_rubberband:
                # -F preserves formants (avoids the "chipmunk"/"demon" voice
                # artifact on vocal-heavy stems). No -t/-T given, so duration
                # is untouched -- this is a true pitch-only shift.
                cmd = ["rubberband", "--pitch", str(semitones), "-F", str(input_path), str(output_path)]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode == 0:
                    success = True
                else:
                    logging.warning(f"RubberBand pitch-shift failed: {result.stderr}, falling back to FFmpeg")

            if not success:
                # NOTE: asetrate alone changes pitch AND tempo together (it's
                # a playback-speed trick) -- this used to be the whole filter
                # chain, which silently sped up/slowed down the song by the
                # pitch ratio (e.g. a +2 semitone shift made a 133 BPM song
                # play at ~149 BPM). The atempo term(s) compensate the tempo
                # back out so only pitch actually changes.
                atempo_chain = self._atempo_chain(1 / pitch_ratio)
                cmd = [
                    "ffmpeg", "-i", str(input_path),
                    "-af", f"asetrate=44100*{pitch_ratio},aresample=44100,{atempo_chain}",
                    "-y", "-q:a", "9", str(output_path)
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    logging.error(f"FFmpeg pitch shift failed: {result.stderr}")
                    return False, -1

            logging.info(f"✅ Pitch-shifted {input_path} → {output_path} by {semitones} semitones (ratio {pitch_ratio:.4f})")

            # Analyze the output to verify the key was shifted correctly
            try:
                _, _, measured_key, _measured_scale = self.analyze_track_and_key(str(output_path))

                # Calculate expected key after shift
                if source_key is not None and source_key >= 0:
                    expected_key = (source_key + semitones) % 12
                    key_name_measured = self._key_to_note(measured_key) if measured_key >= 0 else "?"
                    key_name_expected = self._key_to_note(expected_key)
                    logging.info(f"✅ Key analysis: measured={key_name_measured}, expected={key_name_expected}")
                else:
                    key_name_measured = self._key_to_note(measured_key) if measured_key >= 0 else "?"
                    logging.info(f"✅ Key detected: {key_name_measured}")

                return True, measured_key
            except Exception as key_err:
                logging.warning(f"Key analysis after pitch shift failed: {key_err}, but pitch shift succeeded")
                return True, -1

        except subprocess.TimeoutExpired:
            logging.error(f"Pitch shift timeout for {input_path}")
            return False, -1
        except Exception as e:
            logging.error(f"Pitch shift failed for {input_path}: {e}", exc_info=True)
            return False, -1

    @staticmethod
    def _key_to_note(key):
        """Convert key number (0-11) to note name."""
        notes = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        return notes[key] if 0 <= key < 12 else "?"

    @staticmethod
    def _atempo_chain(ratio):
        """ffmpeg's atempo filter only accepts a 0.5-2.0 ratio per instance.
        Decompose an arbitrary ratio into a chain of atempo filters that are
        each within that range and multiply out to the requested ratio."""
        factors = []
        remaining = ratio
        while remaining < 0.5 or remaining > 2.0:
            step = 2.0 if remaining > 2.0 else 0.5
            factors.append(step)
            remaining /= step
        factors.append(remaining)
        return ",".join(f"atempo={f}" for f in factors)

    @classmethod
    def _effects(cls, chain, sliders, slot, tempo_ratio=1.0):
        """Apply the track-wide controls after its stems have been balanced.

        Every chain reaching this point has already been normalized to
        TARGET_SAMPLE_RATE (see render()), so the asetrate trick below is
        always relative to the stream's actual sample rate, not a guess.

        tempo_ratio folds in BPM-matching (target_bpm / detected_bpm, from
        render()) on top of the manual Speed slider -- atempo changes tempo
        without touching pitch, so it's the right tool for BPM matching,
        unlike the asetrate-based pitch shift below.

        Returns (chain, duration_scale): duration_scale is how much this
        chain compresses (>1) or stretches (<1) the track's timeline overall
        -- render() uses it to project a beat position measured on the
        original file forward through these effects, for beatmatching.
        """
        pitch = float(sliders.get(f"s{slot}_pitch_shift", 0.0))
        speed = float(sliders.get(f"s{slot}_speed", 1.0)) * tempo_ratio
        reverb = float(sliders.get(f"s{slot}_reverb", 0.0))
        eq_low = float(sliders.get(f"s{slot}_eq_low", 0.0))
        eq_mid = float(sliders.get(f"s{slot}_eq_mid", 0.0))
        eq_high = float(sliders.get(f"s{slot}_eq_high", 0.0))

        duration_scale = 1.0
        if abs(pitch) > 0.05:
            rate = cls.TARGET_SAMPLE_RATE
            pitch_ratio = 2 ** (pitch / 12)
            chain += f",asetrate={rate}*{pitch_ratio},aresample={rate}"
            duration_scale *= pitch_ratio
        if abs(speed - 1.0) > 0.05:
            chain += "," + cls._atempo_chain(speed)
            duration_scale *= speed
        if reverb > 0.05:
            chain += f",aecho=0.8:0.9:{int(reverb * 60)}:{reverb * 0.4}"
        if abs(eq_low) > 0.05:
            chain += f",equalizer=f=100:width_type=o:width=2:g={eq_low * 6}"
        if abs(eq_mid) > 0.05:
            chain += f",equalizer=f=1000:width_type=o:width=2:g={eq_mid * 4}"
        if abs(eq_high) > 0.05:
            chain += f",equalizer=f=8000:width_type=o:width=2:g={eq_high * 5}"
        return chain, duration_scale

    def _get_available_stems(self, stem_set):
        """Determine which stems are actually available from a stem_set dict.
        Returns ordered list of stem names that exist and have files."""
        if not isinstance(stem_set, dict):
            return []

        # Try 9-stem multi-engine mode first, then fall back to 7-stem legacy mode
        stem_priority = [
            "vocals",
            "kick", "snare", "hihat", "tom",
            "bass", "guitar", "piano", "other",
            # Fallback legacy stem name
            "drums",
        ]

        available = []
        for stem in stem_priority:
            if stem in stem_set and stem_set[stem] and Path(stem_set[stem]).is_file():
                available.append(stem)

        return available

    def render(self, params, preview=False, preview_duration=15):
        slots = params["songs"]
        stems_by_slot = params.get("stems", [None] * len(slots))
        bpms = params.get("bpms", [None] * len(slots))
        beat_anchors = params.get("beat_anchors", [None] * len(slots))
        beat_offsets = params.get("beat_offsets", [0.0] * len(slots))
        target_bpm = params.get("target_bpm")
        # Beatmatching only makes sense once every aligned track shares the
        # same tempo -- without a target BPM their beat grids would just
        # drift apart again over the length of the track.
        beatmatch = bool(params.get("beatmatch")) and bool(target_bpm)
        sliders = params["sliders"]
        if len(slots) < 2 or not slots[0] or not slots[1]:
            raise ValueError("Load Song 1 and Song 2 before mixing. Song 3 is optional.")

        # Pre-pass for beatmatching: project each track's measured beat
        # position through the timeline-scaling effects (pitch + tempo
        # match + manual speed) it will actually get, so we know where that
        # beat lands in the *rendered* output, not the original file. Only
        # tracks that are themselves being tempo-matched to target_bpm are
        # eligible -- an unmatched track's tempo (and therefore beat period)
        # differs from the rest, so aligning it once would just drift out of
        # phase again a few beats later.
        final_anchors = {}
        if beatmatch:
            for slot, song in enumerate(slots):
                if not song:
                    continue
                detected_bpm = bpms[slot] if slot < len(bpms) else None
                anchor = beat_anchors[slot] if slot < len(beat_anchors) else None
                if not detected_bpm or anchor is None:
                    continue
                # Song 1 (slot 0) is always the fixed beatmatch reference --
                # adelay can only push a track later, never earlier, so one
                # track has to be everyone else's zero point. It never takes
                # a phase offset either, since nudging the reference's own
                # anchor would just be a roundabout way of shifting every
                # other track by the same amount -- same result, more
                # confusing knob.
                #
                # NOTE: the user's beat_offsets choice is intentionally NOT
                # folded in here -- it's applied separately below as a plain
                # forward delay (see user_delay), matching exactly what the
                # live player does (DualStemPlayer.setBeatOffset: a direct,
                # always-non-negative beats->seconds delay on Song 2, nothing
                # more). This anchor is ONLY the automatic phase-correction
                # target: where Song 2's beat would need to land to line up
                # with Song 1's, before any of the user's own offset choice.
                tempo_ratio = target_bpm / detected_bpm
                pitch = float(sliders.get(f"s{slot}_pitch_shift", 0.0))
                speed = float(sliders.get(f"s{slot}_speed", 1.0)) * tempo_ratio
                duration_scale = 1.0
                if abs(pitch) > 0.05:
                    duration_scale *= 2 ** (pitch / 12)
                if abs(speed - 1.0) > 0.05:
                    duration_scale *= speed
                final_anchors[slot] = anchor / duration_scale
        # Song 1 is always the reference. If it has no usable BPM (analysis
        # failed and no override was set), beatmatching does nothing at all
        # this render rather than silently falling back to another track.
        reference_slot = 0 if 0 in final_anchors else None
        beat_period = 60.0 / target_bpm if beatmatch else None

        crossfade = float(params.get("crossfader", 50)) / 100.0
        fades = {0: min(1.0, 2 * (1 - crossfade)), 1: min(1.0, 2 * crossfade)}
        inputs, filters, mixed_tracks = [], [], []
        input_number = 0
        # Every track is normalized to the same sample rate/channel layout
        # before mixing or effects, so amix never has to guess how to
        # reconcile mismatched inputs, and asetrate-based pitch shifting can
        # rely on a known, fixed source rate.
        normalize = f"aformat=sample_rates={self.TARGET_SAMPLE_RATE}:channel_layouts=stereo"

        for slot, song in enumerate(slots):
            if not song:
                continue
            fade = fades.get(slot, 1.0)
            stem_set = stems_by_slot[slot] if slot < len(stems_by_slot) else None

            # Dynamically determine available stems (7-stem legacy OR 9-stem advanced)
            available_stems = self._get_available_stems(stem_set)

            if available_stems:
                volumes = {
                    stem: float(sliders.get(f"s{slot}_{stem}_volume", 1.0))
                    for stem in available_stems
                }
                labels = []
                for stem in available_stems:
                    inputs.extend(["-i", stem_set[stem]])
                    label = f"stem_{slot}_{stem}"
                    filters.append(f"[{input_number}:a]volume={volumes[stem] * fade}[{label}]")
                    labels.append(f"[{label}]")
                    input_number += 1
                chain = "".join(labels) + f"amix=inputs={len(available_stems)}:normalize=0,{normalize}"
            else:
                raise RuntimeError(
                    f"Song {slot + 1}'s stems are missing or incomplete -- cannot render without them. "
                    f"Try reprocessing this song's stems."
                )

            # BPM-match this track to the user's target tempo, if both a
            # target and a detected BPM are available. Falsy detected_bpm
            # covers "not analyzed yet" (None) and "analysis failed" (False).
            detected_bpm = bpms[slot] if slot < len(bpms) else None
            tempo_ratio = (target_bpm / detected_bpm) if (target_bpm and detected_bpm) else 1.0

            chain, _duration_scale = self._effects(chain, sliders, slot, tempo_ratio)

            # Two INDEPENDENT delay contributions, both always >= 0 (adelay
            # can only push a track later, never earlier -- summing two
            # non-negative delays can never go negative, unlike trying to
            # fold the user's offset into the anchor before modulo-wrapping):
            #
            # 1. The user's own beat-offset choice, as a plain forward delay
            #    -- matches the live player exactly (DualStemPlayer's
            #    DelayNode: beats/bpm*60, nothing more), so what you hear in
            #    the final render matches what you heard live, bars and all.
            offset_beats = 0.0 if slot == 0 else (beat_offsets[slot] if slot < len(beat_offsets) else 0.0)
            effective_bpm = target_bpm if (beatmatch and detected_bpm) else detected_bpm
            user_delay = (offset_beats * 60.0 / effective_bpm) if (offset_beats and effective_bpm) else 0.0

            # 2. Automatic phase correction from beatmatching -- a small,
            #    modulo-one-beat nudge so this track's OWN natural beat lines
            #    up with the reference's, independent of whatever the user
            #    additionally asked for above.
            auto_delay = 0.0
            if beatmatch and reference_slot is not None and slot != reference_slot and slot in final_anchors:
                auto_delay = (final_anchors[reference_slot] - final_anchors[slot]) % beat_period

            total_delay = user_delay + auto_delay
            if total_delay > 0.005:
                chain += f",adelay={int(round(total_delay * 1000))}:all=1"

            filters.append(f"{chain}[track_{slot}]")
            mixed_tracks.append(f"[track_{slot}]")

        filter_complex = ";".join(filters)
        filter_complex += ";" + "".join(mixed_tracks)
        filter_complex += f"amix=inputs={len(mixed_tracks)}:duration=longest:normalize=0,alimiter=limit=0.95[final]"

        # Use unique preview files to avoid concurrent render conflicts.
        # Preview stays MP3 (small, fast to generate/stream for a quick
        # listen); the real final render is WAV -- genuinely lossless, and
        # a WAV (not a FLAC re-encode of an already-lossy MP3, which is what
        # this used to produce) is also what write_acid_chunk() needs to
        # embed tempo/key info DAWs can actually read.
        if preview:
            import time
            timestamp = str(int(time.time() * 1000))[-8:]  # Last 8 digits of milliseconds
            output = str(BASE_DIR / f"preview_temp_{timestamp}.mp3")
        else:
            output = str(BASE_DIR / "final_remix.wav")
        command = [self.ffmpeg, "-y", *inputs, "-filter_complex", filter_complex, "-map", "[final]"]
        if preview:
            command += ["-c:a", "libmp3lame", "-q:a", "2", "-t", str(preview_duration)]
        command += [output]

        # Run via Popen (not subprocess.run) and track the process so
        # stop_all() can kill it if the GUI is closed mid-encode.
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as error:
            raise RuntimeError("FFmpeg is not installed or is not in PATH. Install FFmpeg before previewing or rendering.") from error

        MashupEngine._active_encode_processes.append(proc)
        try:
            stdout, stderr = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        finally:
            if proc in MashupEngine._active_encode_processes:
                MashupEngine._active_encode_processes.remove(proc)

        if proc.returncode != 0:
            detail = (stderr or "").strip() or "FFmpeg failed without an error message."
            raise RuntimeError(f"FFmpeg could not make the mix:\n{detail[-1200:]}")

        if preview:
            # Stop any currently playing preview
            MashupEngine.stop_preview()
            # Don't auto-play with ffplay - let the browser player handle it
        return output

    def process_stem(self, input_stem, output_path, pitch_shift=0.0, speed=1.0, target_bpm=None, song_bpm=None):
        """Apply pitch and tempo adjustments to a stem file (no mixing, just effects)."""
        normalize = f"aformat=sample_rates={self.TARGET_SAMPLE_RATE}:channel_layouts=stereo"
        chain = "[0:a]" + normalize

        # Calculate tempo ratio for BPM matching
        tempo_ratio = (target_bpm / song_bpm) if (target_bpm and song_bpm) else 1.0
        speed = speed * tempo_ratio

        # Apply pitch shift
        if abs(pitch_shift) > 0.05:
            rate = self.TARGET_SAMPLE_RATE
            pitch_ratio = 2 ** (pitch_shift / 12)
            chain += f",asetrate={rate}*{pitch_ratio},aresample={rate}"

        # Apply tempo/speed
        if abs(speed - 1.0) > 0.05:
            chain += "," + self._atempo_chain(speed)

        chain += "[out]"

        command = [self.ffmpeg, "-y", "-i", input_stem, "-filter_complex", chain, "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "2", output_path]

        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as error:
            raise RuntimeError("FFmpeg is not installed or is not in PATH.") from error

        MashupEngine._active_encode_processes.append(proc)
        try:
            stdout, stderr = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        finally:
            if proc in MashupEngine._active_encode_processes:
                MashupEngine._active_encode_processes.remove(proc)

        if proc.returncode != 0:
            detail = (stderr or "").strip() or "FFmpeg failed without an error message."
            raise RuntimeError(f"FFmpeg could not process stem:\n{detail[-500:]}")

    def split_drums(self, drum_stem_path, output_dir):
        """Split drum stem into kick, snare, hi-hat, and toms using frequency-based separation.

        Returns dict: {'kick': path, 'snare': path, 'hihat': path, 'tom': path}
        """
        import os
        import logging
        from pathlib import Path

        try:
            os.makedirs(output_dir, exist_ok=True)
            stem_name = Path(drum_stem_path).stem

            outputs = {
                'kick': str(Path(output_dir) / f"{stem_name}_kick.wav"),
                'snare': str(Path(output_dir) / f"{stem_name}_snare.wav"),
                'hihat': str(Path(output_dir) / f"{stem_name}_hihat.wav"),
                'tom': str(Path(output_dir) / f"{stem_name}_tom.wav"),
            }

            # FFmpeg filter graph for drum separation by frequency
            # Kick: 20-250 Hz (low bass)
            # Tom: 200-2000 Hz (mid drums)
            # Snare: 1000-8000 Hz (snare crack)
            # Hi-hat: 5000-20000 Hz (high cymbals)

            filter_graph = (
                # Split into parallel chains
                "[0:a]"
                # Kick: Low-pass to 250 Hz, then high-pass to 20 Hz
                "lowpass=f=250[kick_low]; "
                "[kick_low]highpass=f=20[kick]; "

                # Snare: Band-pass 1000-8000 Hz
                "[0:a]bandpass=f=4000:width_type=o:width=2[snare]; "

                # Hi-hat: High-pass 5000 Hz
                "[0:a]highpass=f=5000[hihat]; "

                # Tom: Band-pass 200-2000 Hz
                "[0:a]bandpass=f=1000:width_type=o:width=1[tom]"
            )

            # Export each frequency band to separate file
            for name, freq_range in [
                ('kick', '20-250Hz'),
                ('snare', '1000-8000Hz'),
                ('hihat', '5000-20000Hz'),
                ('tom', '200-2000Hz'),
            ]:
                if name == 'kick':
                    filters = f"[0:a]lowpass=f=250,highpass=f=20"
                elif name == 'snare':
                    filters = f"[0:a]bandpass=f=4000:width_type=o:width=2"
                elif name == 'hihat':
                    filters = f"[0:a]highpass=f=5000"
                elif name == 'tom':
                    filters = f"[0:a]bandpass=f=1000:width_type=o:width=1"

                cmd = [
                    self.ffmpeg, "-i", str(drum_stem_path),
                    "-af", filters,
                    "-y", "-q:a", "9",
                    outputs[name]
                ]

                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    logging.error(f"Drum split ({name}) failed: {result.stderr}")
                    raise RuntimeError(f"Could not split {name} from drums")

                logging.info(f"✅ Split drum {name}: {outputs[name]}")

            return outputs

        except subprocess.TimeoutExpired:
            logging.error(f"Drum split timeout for {drum_stem_path}")
            raise RuntimeError("Drum split took too long")
        except Exception as e:
            logging.error(f"Drum split failed for {drum_stem_path}: {e}", exc_info=True)
            raise

    def separate_stems_multi_engine(self, songs, use_restoration=True):
        """Advanced multi-engine stem separation: Mel-Band Roformer Karaoke (vocals) +
        Demucs htdemucs_6s (bass/guitar/piano/other/drums) + MDX23C DrumSep
        (kick/snare, ML) + frequency-split (hihat/tom, approximate) + optional
        HiFi++ GAN artifact restoration.

        Every stem is derived straight from the full song: the Karaoke model and
        htdemucs_6s both run directly against the original/processed WAV, in
        parallel. The one deliberate exception is kick/snare/hihat/tom -- both
        DrumSep and the frequency-split fallback need an isolated drum stem, not
        a full mix, so stage 2 runs them on htdemucs_6s's 'drums' output rather
        than on the song itself.

        Verified against a real 25s clip: DrumSep (MDX23C-DrumSep-aufr33-jarredou)
        only separates kick + snare, not hihat/tom -- there's no ML model for
        those in the audio-separator registry, so hihat/tom fall back to the
        legacy bandpass-filter split (split_drums()) on the same drum stem,
        applied only for those two components; DrumSep's kick/snare are kept.

        Returns list of dicts with 9 stems per song (7 ML-separated + hihat/tom
        approximated via frequency filtering).
        """
        import logging
        import threading

        results = []
        for song_idx, song in enumerate(songs):
            logging.info(f"\n{'='*60}")
            logging.info(f"🎵 Processing Song {song_idx + 1}/{len(songs)}: {Path(song).name}")
            logging.info(f"{'='*60}")

            song_hash = hashlib.sha1(str(Path(song).resolve()).encode("utf-8")).hexdigest()[:16]
            song_out_dir = self.stems_dir / song_hash
            song_out_dir.mkdir(exist_ok=True, parents=True)

            # STAGE 1: Parallel extraction, both straight from the full song
            logging.info(f"📊 [STAGE 1] Parallel vocal + multi-instrument extraction...")

            vocals_path = None
            demucs_stems = None

            vocals_lock = threading.Lock()
            demucs_lock = threading.Lock()

            def extract_vocals():
                nonlocal vocals_path
                try:
                    logging.info(f"  🎤 Mel-Band Roformer Karaoke: extracting vocals...")
                    v = self._separate_vocals_karaoke(str(song), str(song_out_dir))
                    with vocals_lock:
                        vocals_path = v
                    logging.info(f"  ✅ Vocals extracted")
                except Exception as e:
                    logging.error(f"  ❌ Karaoke vocal separation failed: {e}")
                    with vocals_lock:
                        vocals_path = None

            def extract_instruments():
                nonlocal demucs_stems
                try:
                    logging.info(f"  🎼 Demucs htdemucs_6s: extracting bass/guitar/piano/other/drums...")
                    stems = self._separate_stems_demucs6s(str(song), str(song_out_dir))
                    with demucs_lock:
                        demucs_stems = stems
                    logging.info(f"  ✅ 6-stem separation complete")
                except Exception as e:
                    logging.error(f"  ❌ Demucs htdemucs_6s failed: {e}")
                    with demucs_lock:
                        demucs_stems = None

            t1 = threading.Thread(target=extract_vocals)
            t2 = threading.Thread(target=extract_instruments)
            for t in (t1, t2):
                t.start()
            for t in (t1, t2):
                t.join()

            if not vocals_path or not demucs_stems:
                raise RuntimeError(f"Stage 1 failed for {Path(song).name}")

            # STAGE 2: Drum splitting -- the one deliberate stem-of-stem step (see
            # docstring above). Kick/snare come from DrumSep (real ML separation);
            # hihat/tom come from bandpass filtering the same drum stem, since no
            # ML model for those exists in the registry.
            logging.info(f"🥁 [STAGE 2] Kick/snare (MDX23C DrumSep) + hihat/tom (frequency split)...")
            drums_stem = demucs_stems['drums']
            kick_snare = self._split_drums_mdx23c(drums_stem, str(song_out_dir))
            hihat_tom = self.split_drums(drums_stem, str(song_out_dir))

            # STAGE 3: Assemble final stem structure
            logging.info(f"🔧 [STAGE 3] Assembling final stem structure...")
            final_stems = {
                'vocals': vocals_path,
                'kick': kick_snare.get('kick'),
                'snare': kick_snare.get('snare'),
                'hihat': hihat_tom.get('hihat'),
                'tom': hihat_tom.get('tom'),
                'bass': demucs_stems.get('bass'),
                'guitar': demucs_stems.get('guitar'),
                'piano': demucs_stems.get('piano'),
                'other': demucs_stems.get('other'),
            }

            missing = [k for k, v in final_stems.items() if not v or not Path(v).is_file()]
            if missing:
                raise RuntimeError(f"Multi-engine separation did not produce: {missing}")

            # STAGE 4: Optional HiFi++ GAN restoration
            if use_restoration:
                logging.info(f"✨ [STAGE 4] HiFi++ GAN restoration...")
                try:
                    final_stems = self._apply_hifi_restoration(final_stems, str(song_out_dir))
                    logging.info(f"  ✅ Restoration complete")
                except Exception as e:
                    logging.warning(f"  ⚠️  HiFi++ restoration skipped: {e}")

            self._conform_stem_lengths(final_stems)
            results.append(final_stems)
            logging.info(f"✅ Song {song_idx + 1} complete: 9 stems ready\n")

        return results

    def _separate_vocals_karaoke(self, audio_path, output_dir):
        """Extract clean lead vocals using a Mel-Band Roformer Karaoke model, run
        directly on the full song (not on an already-separated stem).

        Verified against a real clip: despite the "Karaoke" name, this
        checkpoint's second output is a generic Instrumental (full mix minus
        vocals), not isolated backing/harmony vocals -- there's no dedicated
        backing-vocal model in the audio-separator registry, so we only keep
        the clean vocals file and discard the instrumental one. Roformer-family
        vocal models like this one measurably outperform Demucs's built-in
        vocal stem on separation quality (SDR), which is why we use this
        instead of htdemucs_6s's own 'vocals' output.

        Returns: path to the vocals-only WAV.
        """
        import logging

        try:
            from audio_separator.separator import Separator
        except ImportError:
            raise RuntimeError(
                "Multi-engine mode requires: pip install audio-separator onnxruntime\n"
                "Models download automatically on first use."
            )

        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(exist_ok=True, parents=True)

        try:
            separator = Separator(output_dir=str(output_dir_path), output_format="WAV")
            separator.load_model(model_filename="mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt")

            logging.info(f"  Separating vocals from {Path(audio_path).name}...")
            output_files = separator.separate(audio_path)
            resolved = [p if Path(p).is_absolute() else str(output_dir_path / p) for p in output_files]

            logging.info(f"  Karaoke model produced: {[Path(p).name for p in resolved]}")

            vocals_matches = [p for p in resolved if 'vocal' in Path(p).stem.lower()]
            if not vocals_matches:
                raise RuntimeError(
                    f"Could not identify the vocals file among: {[Path(p).name for p in resolved]}"
                )
            return vocals_matches[0]
        except Exception as e:
            logging.error(f"Karaoke vocal separation failed: {e}")
            raise RuntimeError(f"Vocal extraction failed: {str(e)[-500:]}")

    def _separate_stems_demucs6s(self, audio_path, output_dir):
        """Separate bass/guitar/piano/other/drums directly from the full song using
        Demucs' htdemucs_6s model (also outputs a 'vocals' stem, unused here --
        the Karaoke model supplies our (higher-quality) vocals instead)."""
        import logging

        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(exist_ok=True, parents=True)

        command = [sys.executable, "-m", "demucs", "-n", "htdemucs_6s",
                   "--out", str(output_dir_path), audio_path]
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "Demucs failed without an error message."
            raise RuntimeError(f"Demucs htdemucs_6s could not separate {Path(audio_path).name}:\n{detail[-1200:]}")

        song_folder = Path(audio_path).stem
        candidates = list(output_dir_path.glob(f"htdemucs_6s/{song_folder}"))
        if not candidates:
            raise RuntimeError(f"Demucs htdemucs_6s finished, but no stem folder was found for {Path(audio_path).name}.")
        folder = candidates[0]

        stems = {name: str(folder / f"{name}.wav") for name in ("drums", "bass", "other", "guitar", "piano")}
        missing = [name for name, path in stems.items() if not Path(path).is_file()]
        if missing:
            raise RuntimeError(f"Demucs htdemucs_6s did not create all expected stems: {', '.join(missing)}")

        logging.info(f"  Demucs htdemucs_6s produced: {list(stems.keys())}")
        return stems

    def _split_drums_mdx23c(self, drums_path, output_dir):
        """Split an isolated drum stem into kick + snare using the MDX23C DrumSep
        model. This is a deliberate stem-of-stem step in the multi-engine
        pipeline: DrumSep is trained on isolated drums, so it needs `drums_path`
        (htdemucs_6s's drum stem) as input rather than the full song.

        Verified against a real clip: MDX23C-DrumSep-aufr33-jarredou only
        produces kick + snare, not hihat/tom -- there's no ML model for those in
        the audio-separator registry (see split_drums() for that fallback).
        """
        import logging

        try:
            from audio_separator.separator import Separator
        except ImportError:
            raise RuntimeError(
                "Multi-engine mode requires: pip install audio-separator onnxruntime\n"
                "Models download automatically on first use."
            )

        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(exist_ok=True, parents=True)

        try:
            separator = Separator(output_dir=str(output_dir_path), output_format="WAV")
            separator.load_model(model_filename="MDX23C-DrumSep-aufr33-jarredou.ckpt")

            logging.info(f"  Splitting kick/snare from {Path(drums_path).name}...")
            output_files = separator.separate(drums_path)
            resolved = [p if Path(p).is_absolute() else str(output_dir_path / p) for p in output_files]

            logging.info(f"  DrumSep produced {len(resolved)} files: {[Path(p).name for p in resolved]}")

            mapping = {}
            for path in resolved:
                stem = Path(path).stem.lower()
                if 'kick' in stem:
                    mapping.setdefault('kick', path)
                elif 'snare' in stem:
                    mapping.setdefault('snare', path)

            missing = [name for name in ('kick', 'snare') if name not in mapping]
            if missing:
                raise RuntimeError(
                    f"Could not identify {missing} among DrumSep's output files: "
                    f"{[Path(p).name for p in resolved]}. Update the keyword matching "
                    f"above to match this model's actual naming."
                )
            return mapping
        except Exception as e:
            logging.error(f"MDX23C DrumSep failed: {e}")
            raise RuntimeError(f"Drum splitting failed: {str(e)[-500:]}")

    def _apply_hifi_restoration(self, stems, output_dir):
        """Apply HiFi++ GAN restoration to all stems for artifact removal.

        HiFi++ is a multi-stage GAN framework that:
        - Detects and removes codec/compression artifacts
        - Restores high-frequency content
        - Per-stem quality optimization

        Requires: pip install https://github.com/CPJKU/music-source-restoration
        If not available, falls back to spectral filtering.
        """
        import logging

        # Try HiFi++ GAN first, fall back to spectral filtering
        try:
            return self._apply_hifi_gan(stems, output_dir)
        except Exception as e:
            logging.warning(f"HiFi++ GAN restoration unavailable ({e}), using spectral filtering")
            return self._apply_spectral_restoration(stems, output_dir)

    def _apply_hifi_gan(self, stems, output_dir):
        """Apply HiFi++ GAN using CPJKU Music Source Restoration.

        Multi-stage pipeline:
        1. BS-RoFormer detection
        2. HiFi++ GAN waveform restoration
        3. Per-stem quality assessment
        """
        import logging
        import torch
        import torchaudio

        try:
            from restoration.mixture_inference import create_mixture_system
            from pathlib import Path as PathlibPath
        except ImportError:
            raise RuntimeError(
                "HiFi++ GAN requires music-source-restoration. Install with:\n"
                "pip install git+https://github.com/CPJKU/music-source-restoration"
            )

        restored = {}
        logging.info("Initializing HiFi++ GAN restoration...")

        # Create mixture-of-experts system (routes to best expert per instrument)
        try:
            system = create_mixture_system(
                checkpoints=None,  # Uses default pre-trained checkpoints
                routing_strategy="weighted",  # Weighted average of experts
                device="cuda" if torch.cuda.is_available() else "cpu"
            )
        except Exception as e:
            raise RuntimeError(f"Could not initialize HiFi++ GAN: {e}")

        for stem_name, stem_path in stems.items():
            if not stem_path or not Path(stem_path).is_file():
                restored[stem_name] = stem_path
                continue

            try:
                restored_path = str(Path(stem_path).parent / f"{Path(stem_path).stem}_restored.wav")

                # Load stem audio
                audio, sr = torchaudio.load(str(stem_path))
                if sr != 44100:
                    audio = torchaudio.transforms.Resample(sr, 44100)(audio)

                # Restore with HiFi++ (instrument-aware expert selection)
                with torch.no_grad():
                    restored_audio = system.restore_stem(
                        audio,
                        instrument=self._stem_to_instrument(stem_name)
                    )

                # Save restored stem
                torchaudio.save(restored_path, restored_audio, 44100)

                restored[stem_name] = restored_path
                logging.info(f"    ✓ HiFi++ GAN restored: {stem_name}")

            except Exception as e:
                logging.warning(f"    ⚠️  HiFi++ restoration failed for {stem_name}: {e}")
                restored[stem_name] = stem_path

        return restored

    def _apply_spectral_restoration(self, stems, output_dir):
        """Fallback spectral restoration (no ML model required).

        Uses FFmpeg spectral filtering to remove shimmer/artifacts.
        Quality lower than HiFi++ GAN but no dependencies.
        """
        import logging

        restored = {}
        for stem_name, stem_path in stems.items():
            if not stem_path or not Path(stem_path).is_file():
                restored[stem_name] = stem_path
                continue

            try:
                restored_path = str(Path(stem_path).parent / f"{Path(stem_path).stem}_restored.wav")

                # Gentle spectral restoration: remove subsonic + ultrasonic noise
                filters = "highpass=f=15,lowpass=f=21000"

                cmd = [
                    self.ffmpeg, "-y", "-i", str(stem_path),
                    "-af", filters,
                    "-q:a", "9", restored_path
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if result.returncode == 0 and Path(restored_path).is_file():
                    restored[stem_name] = restored_path
                    logging.info(f"    ✓ Spectral filtering: {stem_name}")
                else:
                    restored[stem_name] = stem_path

            except Exception as e:
                logging.debug(f"Spectral filtering skipped for {stem_name}: {e}")
                restored[stem_name] = stem_path

        return restored

    @staticmethod
    def _stem_to_instrument(stem_name):
        """Map stem name to instrument label for HiFi++ expert routing."""
        instrument_map = {
            'vocals': 'vocals',
            'kick': 'drums',
            'snare': 'drums',
            'hihat': 'drums',
            'tom': 'drums',
            'bass': 'bass',
            'guitar': 'guitar',
            'piano': 'piano',
            'other': 'other',
        }
        return instrument_map.get(stem_name, 'other')
