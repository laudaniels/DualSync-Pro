import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class MashupEngine:
    """Build FFmpeg mixes and optionally prepare Demucs source stems."""

    STEM_NAMES = ("vocals", "drums", "bass", "other")
    TARGET_SAMPLE_RATE = 44100

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

    def separate_stems(self, songs):
        """Run Demucs once for each source and return its produced stem paths."""
        if not shutil.which("demucs"):
            # `python -m demucs` is the supported fallback when its Scripts
            # directory is not included in PATH.
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
            # Each song gets its own output folder keyed by its full resolved
            # path, so two different songs that happen to share a base
            # filename (e.g. "track.mp3" from different folders) never
            # collide or overwrite each other's stems.
            song_hash = hashlib.sha1(str(Path(song).resolve()).encode("utf-8")).hexdigest()[:16]
            song_out_dir = self.stems_dir / song_hash
            command = [sys.executable, "-m", "demucs", "--out", str(song_out_dir), song]
            result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "Demucs failed without an error message."
                raise RuntimeError(f"Demucs could not separate {Path(song).name}:\n{detail[-1200:]}")

            # Demucs writes: <song_out_dir>/<model>/<original filename>/<stem>.wav
            song_folder = Path(song).stem
            candidates = list(song_out_dir.glob(f"*/{song_folder}"))
            if not candidates:
                raise RuntimeError(f"Demucs finished, but no stem folder was found for {Path(song).name}.")
            folder = candidates[0]
            stems = {name: str(folder / f"{name}.wav") for name in self.STEM_NAMES}
            missing = [name for name, path in stems.items() if not Path(path).is_file()]
            if missing:
                raise RuntimeError(f"Demucs did not create all expected stems for {Path(song).name}: {', '.join(missing)}")
            results.append(stems)
        return results

    def analyze_track(self, song_path):
        """Estimate a track's tempo (BPM) and a reference beat position.

        Uses Madmom neural network for improved accuracy (±0.5 BPM vs ±2-3 BPM).
        Falls back to Librosa if Madmom unavailable.

        Multi-pass: samples 3 sections of track and returns median BPM for robustness.
        """
        import logging
        import numpy as np

        # Try Madmom first (better accuracy)
        try:
            from madmom.features.beats import RNNBeatProcessor, BeatTrackingProcessor

            logging.info(f"Using Madmom for BPM detection: {song_path}")
            processor = RNNBeatProcessor()
            beat_detector = BeatTrackingProcessor()

            # Process audio
            activations = processor(str(song_path))
            beats = beat_detector(activations)

            if len(beats) > 0:
                # Calculate BPM from beat intervals
                beat_intervals = np.diff(beats[:min(100, len(beats))])
                tempo = 60.0 / np.median(beat_intervals) if np.median(beat_intervals) > 0 else 120.0
                beat_anchor = float(beats[0])

                logging.info(f"✅ Madmom BPM: {tempo:.1f}, beat anchor: {beat_anchor:.2f}s")
                return tempo, beat_anchor
            else:
                logging.warning("Madmom: No beats detected, falling back to Librosa")

        except Exception as e:
            logging.warning(f"Madmom BPM detection failed: {e}, falling back to Librosa")

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
        """Detect BPM and key in a single pass (single audio load).
        Returns tuple: (bpm, beat_anchor, key).
        Key is 0-11 (C=0, C#=1, ..., B=11), or -1 if detection fails."""
        import logging
        import numpy as np
        import librosa

        logging.info(f"Analyzing BPM and key for: {song_path}")

        # Load audio once for both analyses
        try:
            y, sr = librosa.load(song_path, sr=None, mono=True)
        except Exception as e:
            logging.error(f"Failed to load audio: {e}")
            return 120.0, 0.0, -1

        # BPM detection using Madmom first (better accuracy)
        bpm = None
        beat_anchor = None
        try:
            from madmom.features.beats import RNNBeatProcessor, BeatTrackingProcessor

            logging.info("Using Madmom for BPM detection")
            processor = RNNBeatProcessor()
            beat_detector = BeatTrackingProcessor()
            activations = processor(str(song_path))
            beats = beat_detector(activations)

            if len(beats) > 0:
                beat_intervals = np.diff(beats[:min(100, len(beats))])
                bpm = 60.0 / np.median(beat_intervals) if np.median(beat_intervals) > 0 else 120.0
                beat_anchor = float(beats[0])
                logging.info(f"✅ Madmom BPM: {bpm:.1f}, beat anchor: {beat_anchor:.2f}s")
        except Exception as e:
            logging.warning(f"Madmom failed: {e}, using Librosa")

        # Fallback to Librosa if Madmom failed or unavailable
        if bpm is None:
            try:
                # Multi-pass for robustness
                total_duration = librosa.get_duration(y=y, sr=sr)
                bpm_samples = []
                beat_anchors_list = []

                if total_duration > 120.0:
                    offsets = [10.0, total_duration / 2 - 30.0, total_duration - 60.0]
                elif total_duration > 60.0:
                    offsets = [5.0, total_duration - 50.0]
                else:
                    offsets = [0.0]

                for offset in offsets:
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
                    except Exception as e:
                        logging.warning(f"Sample @ {offset:.0f}s failed: {e}")

                if bpm_samples:
                    bpm = float(np.median(bpm_samples))
                    beat_anchor = beat_anchors_list[len(bpm_samples) // 2]
                    logging.info(f"✅ Librosa BPM (median): {bpm:.1f}, beat anchor: {beat_anchor:.2f}s")
                else:
                    bpm = 120.0
                    beat_anchor = 0.0
                    logging.warning("All BPM samples failed")
            except Exception as e:
                logging.error(f"Librosa BPM failed: {e}")
                bpm = 120.0
                beat_anchor = 0.0

        # Key detection from already-loaded audio
        key = -1
        try:
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
            chroma_mean = chroma.mean(axis=1)
            key = int(np.argmax(chroma_mean))
            logging.info(f"✅ Key detected: {self._key_to_note(key)}")
        except Exception as e:
            logging.warning(f"Librosa key detection failed: {e}")

            # Fallback to Essentia
            try:
                from essentia.standard import MonoLoader, KeyExtractor

                loader = MonoLoader(filename=song_path, sampleRate=44100)
                audio = loader()
                key_extractor = KeyExtractor()
                key_str, confidence = key_extractor(audio)

                if key_str and confidence > 0.5:
                    key_notes = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
                    key_name = key_str.split()[0]
                    if key_name in key_notes:
                        key = key_notes.index(key_name)
                        logging.info(f"✅ Key via Essentia: {key_name} (confidence: {confidence:.2f})")
            except Exception as e:
                logging.warning(f"Essentia key detection also failed: {e}")

        return bpm, beat_anchor, key

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
            bpm_tolerance = 0.5
            measured_bpm = None
            use_rubberband = shutil.which("rubberband") is not None

            if use_rubberband:
                logging.info("🎼 Using RubberBand for time-stretching (commercial quality)")
            else:
                logging.warning("⚠️  RubberBand not found, falling back to FFmpeg atempo")

            for pass_num in range(1, max_passes + 1):
                tempo_ratio = target_bpm / current_source_bpm
                temp_output = output_path if pass_num == max_passes else str(Path(output_path).parent / f"{Path(output_path).stem}_pass{pass_num}.wav")

                # Pass 1: Always use FFmpeg for stability; RubberBand produces corrupted output on first pass
                # Pass 2+: Try RubberBand if available
                try_rubberband = use_rubberband and pass_num > 1

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
                        logging.warning("RubberBand failed on Pass {pass_num}, retrying with FFmpeg...")
                        pass_num -= 1
                        continue
                    return False, None

                # Analyze output BPM
                measured_bpm, _ = self.analyze_track(temp_output)

                # Safety check: if BPM detection fails (returns 0), mark as error
                if measured_bpm <= 0:
                    logging.error(f"BPM analysis failed (got {measured_bpm}), output file may be corrupted")
                    if use_rubberband:
                        logging.warning("RubberBand output failed, retrying with FFmpeg...")
                        use_rubberband = False
                        pass_num -= 1
                        continue
                    return False, None

                bpm_error = abs(measured_bpm - target_bpm)
                logging.info(f"Pass {pass_num}: Output BPM = {measured_bpm:.1f}, Error = {bpm_error:.2f} BPM")

                if bpm_error <= bpm_tolerance:
                    # Converged! Copy to final output if needed
                    if pass_num < max_passes:
                        shutil.copy2(temp_output, output_path)
                        for p in range(1, pass_num + 1):
                            temp = Path(output_path).parent / f"{Path(output_path).stem}_pass{p}.wav"
                            temp.unlink(missing_ok=True)
                    logging.info(f"✅ Time-stretched to {measured_bpm:.1f} BPM (target: {target_bpm}) in {pass_num} pass(es)")
                    return True, measured_bpm

                # Prepare for next pass
                if pass_num < max_passes:
                    current_input = temp_output
                    current_source_bpm = measured_bpm
                else:
                    # Last pass - clean up temp files
                    for p in range(1, max_passes):
                        temp = Path(output_path).parent / f"{Path(output_path).stem}_pass{p}.wav"
                        temp.unlink(missing_ok=True)
                    logging.info(f"✅ Time-stretched to {measured_bpm:.1f} BPM (target: {target_bpm}) after {max_passes} passes")
                    return True, measured_bpm

            return False, None

        except subprocess.TimeoutExpired:
            logging.error(f"Time stretch timeout for {input_path}")
            return False, None
        except Exception as e:
            logging.error(f"Time stretch failed for {input_path}: {e}", exc_info=True)
            return False, None

    def pitch_shift_audio(self, input_path, output_path, semitones):
        """Pitch-shift audio using FFmpeg asetrate filter (faster than librosa).
        Positive semitones = pitch up, negative = pitch down.
        Returns True if successful, False if failed."""
        import logging
        import os
        import subprocess

        try:
            logging.info(f"Pitch-shifting {input_path} by {semitones} semitones")

            # Ensure output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Use FFmpeg asetrate filter (much faster than librosa pitch_shift)
            # pitch shift: rate = 2^(semitones/12)
            pitch_ratio = 2 ** (semitones / 12.0)

            cmd = [
                "ffmpeg", "-i", str(input_path),
                "-af", f"asetrate=44100*{pitch_ratio},aresample=44100",
                "-y", "-q:a", "9", str(output_path)
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                logging.info(f"✅ Pitch-shifted {input_path} → {output_path} by {semitones} semitones (ratio {pitch_ratio:.4f})")
                return True
            else:
                logging.error(f"FFmpeg pitch shift failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            logging.error(f"Pitch shift timeout for {input_path}")
            return False
        except Exception as e:
            logging.error(f"Pitch shift failed for {input_path}: {e}", exc_info=True)
            return False

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
                # confusing knob. Expressed in each other song's own native
                # tempo. Alignment below is modulo one beat_period, so only
                # the fractional part shifts anything audible -- whole-beat
                # offsets land on an equivalent beat and cancel out. This is
                # beat-level phase correction, not bar/downbeat selection.
                offset_beats = 0.0 if slot == 0 else (beat_offsets[slot] if slot < len(beat_offsets) else 0.0)
                anchor = anchor + offset_beats * (60.0 / detected_bpm)
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
            # Crossfader balance (song 1/2 only) — stem volumes now handle all level control
            fade = fades.get(slot, 1.0)
            stem_set = stems_by_slot[slot] if slot < len(stems_by_slot) else None
            valid_stems = (isinstance(stem_set, dict) and
                           all(name in stem_set and Path(stem_set[name]).is_file() for name in self.STEM_NAMES))

            if valid_stems:
                volumes = {
                    "vocals": float(sliders.get(f"s{slot}_vocals_vol", 1.0)),
                    "drums": float(sliders.get(f"s{slot}_beats_vol", 1.0)),
                    "bass": float(sliders.get(f"s{slot}_bass_vol", 1.0)),
                    "other": float(sliders.get(f"s{slot}_other_vol", 1.0)),
                }
                labels = []
                for stem in self.STEM_NAMES:
                    inputs.extend(["-i", stem_set[stem]])
                    label = f"stem_{slot}_{stem}"
                    filters.append(f"[{input_number}:a]volume={volumes[stem] * fade}[{label}]")
                    labels.append(f"[{label}]")
                    input_number += 1
                chain = "".join(labels) + f"amix=inputs=4:normalize=0,{normalize}"
            else:
                inputs.extend(["-i", song])
                total_volume = (
                    float(sliders.get(f"s{slot}_vocals_vol", 1.0)) +
                    float(sliders.get(f"s{slot}_beats_vol", 1.0)) +
                    float(sliders.get(f"s{slot}_bass_vol", 1.0))
                ) / 3 * fade
                chain = f"[{input_number}:a]volume={total_volume},{normalize}"
                input_number += 1

            # BPM-match this track to the user's target tempo, if both a
            # target and a detected BPM are available. Falsy detected_bpm
            # covers "not analyzed yet" (None) and "analysis failed" (False).
            detected_bpm = bpms[slot] if slot < len(bpms) else None
            tempo_ratio = (target_bpm / detected_bpm) if (target_bpm and detected_bpm) else 1.0

            chain, _duration_scale = self._effects(chain, sliders, slot, tempo_ratio)

            # Nudge this track's start later (never earlier -- delaying is
            # the only direction adelay can go) so its beat anchor lines up
            # with the reference track's, modulo one beat period.
            if beatmatch and reference_slot is not None and slot != reference_slot and slot in final_anchors:
                delta = (final_anchors[reference_slot] - final_anchors[slot]) % beat_period
                if delta > 0.005:
                    chain += f",adelay={int(round(delta * 1000))}:all=1"

            filters.append(f"{chain}[track_{slot}]")
            mixed_tracks.append(f"[track_{slot}]")

        filter_complex = ";".join(filters)
        filter_complex += ";" + "".join(mixed_tracks)
        filter_complex += f"amix=inputs={len(mixed_tracks)}:duration=longest:normalize=0,alimiter=limit=0.95[final]"

        # Use unique preview files to avoid concurrent render conflicts
        if preview:
            import time
            timestamp = str(int(time.time() * 1000))[-8:]  # Last 8 digits of milliseconds
            output = str(BASE_DIR / f"preview_temp_{timestamp}.mp3")
        else:
            output = str(BASE_DIR / "final_remix.mp3")
        command = [self.ffmpeg, "-y", *inputs, "-filter_complex", filter_complex, "-map", "[final]"]
        command += ["-c:a", "libmp3lame", "-q:a", "2"]
        if preview:
            command += ["-t", str(preview_duration)]
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
