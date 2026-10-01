import hashlib
import logging
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# audio-separator's own default (/tmp/audio-separator-models/) isn't
# persistent -- on this box /tmp gets cleared across restarts, silently
# forcing a ~3 GB re-download of all 4 models (vocals, DrumSep, denoise,
# de-reverb) on the very next run, with no progress reporting for that time.
# Caching under the user's home directory instead survives restarts.
AUDIO_SEPARATOR_MODEL_DIR = Path.home() / ".cache" / "audio-separator-models"


class _AudioSeparatorLogBridge(logging.Handler):
    """Turns audio-separator's own phase-transition log lines (model load
    start, separation start) into progress_callback updates, tagged with
    `prefix` -- so a call site can get "loading model..."/"separating
    audio..." status for free by wrapping a call in
    _report_audio_separator_progress(), instead of audio-separator needing
    to expose a callback of its own (it doesn't)."""

    def __init__(self, prefix, callback):
        super().__init__(level=logging.INFO)
        self._prefix = prefix
        self._callback = callback

    def emit(self, record):
        if not self._callback:
            return
        message = record.getMessage()
        if message.startswith("Loading model"):
            self._callback(f"{self._prefix}: loading model (downloading on first use)...")
        elif message.startswith("Starting separation process"):
            self._callback(f"{self._prefix}: separating audio...")


@contextmanager
def _report_audio_separator_progress(prefix, callback):
    """While the wrapped audio_separator call runs, forward its own log
    lines as progress updates. Safe to nest around a single synchronous
    call -- each audio_separator call this project makes runs to completion
    on its own thread, so there's no cross-talk between the vocals
    call (running in parallel with the separate Demucs subprocess) and
    everything else, which runs sequentially."""
    logger = logging.getLogger("audio_separator.separator.separator")
    handler = _AudioSeparatorLogBridge(prefix, callback)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)


class MashupEngine:
    """Build FFmpeg mixes with multi-engine stem separation: Mel-Band RoFormer
    (lead/backing vocals), Demucs htdemucs_6s (guitar/piano/other, plus bass/
    drums before hdemucs_mmi refines those two), MDX23C DrumSep
    (kick/snare/hihat/tom), denoise + de-reverb (restoration)."""

    STEM_NAMES = ("vocals", "drums", "bass", "other")
    TARGET_SAMPLE_RATE = 44100

    # Appended to every ffmpeg command that writes one of this app's own WAV
    # files, so audio stays 32-bit float all the way through the pipeline
    # instead of ffmpeg's WAV-muxer default (16-bit PCM) -- every model here
    # (Demucs, the vocal RoFormers, MDX23C DrumSep) already computes in
    # float32 internally, so writing float32 at each intermediate hop (the
    # beatgrid warp, each time-stretch pass, drum-frequency splitting, the
    # final render, ...) means none of those hops re-quantizes and loses
    # precision the next stage could have used.
    WAV_CODEC_ARGS = ["-c:a", "pcm_f32le"]

    # How far before a detected kick transient to actually start beat-grid
    # analysis (see _detect_kick_candidates). Cutting an audio array
    # exactly on a hard transient gives beat trackers no quiet lead-in to
    # compute their first frame's onset/spectral-flux value against, which
    # can bias that very first detected beat -- exactly the value used as
    # beat_anchor. Used as the default first attempt, before any BPM
    # estimate is available; see KICK_PRE_ROLL_MIN_SEC/MAX_SEC and
    # KICK_PRE_ROLL_REFINE_MAX_BPM below for the tempo-adaptive refinement
    # applied afterward for slow tracks.
    KICK_PRE_ROLL_SEC = 0.35

    # Once a rough BPM is known (from the first extraction attempt, using
    # KICK_PRE_ROLL_SEC above), slow tracks get the pre-roll refined to one
    # beat's duration (60/bpm), clamped to this range. Measured directly on
    # synthetic audio (kick at a known position, various tempos): at 60 BPM
    # (one beat = 1.0s), anchor error was +0.71s with the fixed 0.35s value
    # vs. +0.42s with the tempo-adaptive one. The clamp matters: an
    # UNCLAMPED full beat (1.0s pre-roll at 60 BPM) was worse still
    # (err -0.50s) -- re-admitting too much of the pre-kick audio confuses
    # the extractor again, so the max caps how far this can help.
    KICK_PRE_ROLL_MIN_SEC = 0.2
    KICK_PRE_ROLL_MAX_SEC = 0.5

    # Only tracks slower than this get the refinement pass above (an extra
    # Essentia call per candidate). 60/bpm only diverges meaningfully from
    # KICK_PRE_ROLL_SEC below roughly 150 BPM, which would mean paying for
    # the extra call on most real music (hip-hop, pop/rock, house/techno --
    # not just genre outliers). Capping it at 100 BPM keeps the extra cost
    # to where the measured benefit was clearest, instead of on most songs.
    KICK_PRE_ROLL_REFINE_MAX_BPM = 100.0

    # How far into the track _detect_kick_candidates searches for a kick
    # transient. Validated against 38 real tracks (validate_pipeline.py):
    # DJ re-edits/extended club mixes routinely push their real beat past
    # the previous 45s cutoff -- a direct A/B on the exact same song showed
    # it plainly (the "Lau Re-Edit" of a track found no candidates at all
    # in the first 45s, while the non-re-edited version of the same song
    # found them fine). This step is cheap regardless of length (plain
    # onset detection, not a repeated Essentia extractor call), so widening
    # it doesn't add meaningful cost -- unlike KICK_PRE_ROLL_REFINE_MAX_BPM
    # above, which gates an actually expensive repeated extraction.
    KICK_SEARCH_MAX_SEC = 90.0

    # Crest-factor (peak / RMS) window and minimum ratio for validating a
    # candidate kick transient (see _detect_kick_candidates). A genuine
    # percussive hit's energy is a short spike far above its surroundings;
    # a sustained drone or pad keeps peak and RMS close together no matter
    # how loud or distorted it's mastered, so this stays reliable even when
    # a spectral-flux baseline (onset_env) would get skewed by one.
    KICK_CREST_WINDOW_SEC = 0.25
    KICK_CREST_FACTOR_MIN = 4.0

    # When more than one kick candidate is found (see _detect_kick_candidates
    # -- e.g. an early sub-bass drop before the track's real downbeat),
    # _detect_beats_essentia tries each in turn and keeps whichever gives the
    # highest RhythmExtractor2013 confidence (no early exit: tested on
    # synthetic audio, a plain "stop at the first 'good' one" rule kept the
    # WRONG candidate, since confidence mostly reflects how periodic the
    # bulk of the shared remaining audio is, not whether this exact trim
    # point is the real downbeat). NOTE: Essentia's confidence for
    # method='multifeature' (what this project uses) is NOT a 0-1 score --
    # it's documented as ranging [0, 5.32], with Essentia's own guidelines
    # reading [0, 1) very low, [1, 1.5] low, (1.5, 3.5] good (~80% accuracy,
    # AMLt measure), (3.5, 5.32] excellent. 1.5 is Essentia's own "good"
    # cutoff (used only for logging now), not an arbitrary guess.
    KICK_CONFIDENCE_GOOD = 1.5

    # Minimum gap between kept kick candidates. Without this, a normal song
    # with drums from the very start (no vague intro at all -- the common
    # case) returns 3 near-duplicate candidates that are just consecutive
    # beats of the same groove (e.g. 0.3s/0.8s/1.3s), tripling the cost of
    # analyze-song for zero benefit, since near-identical trims score
    # near-identical confidence. Extra candidates should only be tried when
    # they plausibly represent a genuinely different section (an early
    # one-off transient vs. the track's real groove), not the next beat.
    KICK_CANDIDATE_MIN_GAP_SEC = 2.0

    # Every audio-separator model this app ever loads (see the 4 Separator(
    # ...).load_model(...) call sites above/below) plus the 2 Demucs presets
    # -- kept in one place so prefetch_models() and any future caller can
    # enumerate them without hunting through separate_stems_multi_engine().
    # (key, filename, label, required) -- `required` marks a model whose
    # absence would break basic app function (nothing else can proceed
    # without it), vs. one that only disables a specific optional feature.
    AUDIO_SEPARATOR_MODEL_FILES = [
        ("vocals_becruily", "mel_band_roformer_vocals_becruily.ckpt", "Vocal model (Mel-Band RoFormer, becruily)", False),
        ("vocals_bsroformer", "model_bs_roformer_ep_368_sdr_12.9628.ckpt", "Vocal model (BS-RoFormer)", False),
        ("drumsep", "MDX23C-DrumSep-aufr33-jarredou.ckpt", "Kick/snare model (MDX23C DrumSep)", False),
        ("denoise", "denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt", "Restoration: denoise model", False),
        ("dereverb", "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt", "Restoration: de-reverb model", False),
    ]

    # Exact size (bytes) of each pinned checkpoint, measured from a known-
    # good download -- used only to catch a truncated/corrupted local file
    # (audio-separator's own download_file_if_not_exists() writes straight
    # to the final path with no atomic rename, so a download killed
    # mid-transfer leaves a permanently "present but broken" file that a
    # bare os.path.isfile() check would wrongly call "cached"). NOT an
    # update mechanism -- these models are pinned by filename (the SDR/epoch
    # suffix IS the version), and upstream publishing a genuine new version
    # would ship under a new filename, not silently replace this one. If a
    # future upstream re-upload ever legitimately changes this exact
    # file's size, update the number here along with it.
    AUDIO_SEPARATOR_MODEL_SIZES = {
        "mel_band_roformer_vocals_becruily.ckpt": 913107578,
        "model_bs_roformer_ep_368_sdr_12.9628.ckpt": 639317465,
        "MDX23C-DrumSep-aufr33-jarredou.ckpt": 437652699,
        "denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt": 913097300,
        "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt": 913107578,
    }
    # (key, model_name, label, required) -- htdemucs is legacy mode's
    # default model, so basic (non-multi-engine) separation needs it;
    # htdemucs_6s and hdemucs_mmi are multi-engine-only.
    DEMUCS_MODELS = [
        ("demucs_htdemucs", "htdemucs", "Demucs (legacy 4-stem model)", True),
        ("demucs_htdemucs6s", "htdemucs_6s", "Demucs (multi-engine 6-stem model)", False),
        ("demucs_hdemucsmmi", "hdemucs_mmi", "Demucs (bass/drums refinement model)", False),
    ]

    # (name, required) metadata for check_environment()'s checks, in the
    # order they run -- kept alongside AUDIO_SEPARATOR_MODEL_FILES/
    # DEMUCS_MODELS purely so all_startup_items() can build the full,
    # up-front "queued" list without running anything. NOTE: if a check is
    # ever added to/removed from check_environment(), update this list too
    # -- there's no other link between the two.
    ENVIRONMENT_CHECK_NAMES = [
        ("ffmpeg", True),
        ("demucs", True),
        ("rubberband", False),
        ("gpu", False),
        ("audio_separator", False),
    ]

    # Guards every audio-separator Separator(...).load_model(...) call for
    # a given model FILE (downloads it on first use if not cached yet).
    # Keyed per-filename rather than one single lock, so prefetch_models()
    # can fetch different models in parallel -- only two attempts at the
    # SAME file need to be mutually exclusive. That matters for Song 1 and
    # Song 2's parallel per-song processing threads: without this, both
    # could see "not cached yet" for the same model at the same moment and
    # both start writing the same destination file at once (audio-
    # separator's own download_file_if_not_exists() has no locking or
    # atomic rename of its own). Class-level so it's shared across the
    # separate MashupEngine() instances each request creates. Only guards
    # the load step, not the actual (slow) separation call that follows --
    # that still runs in parallel across songs/models as intended.
    _model_load_locks = {filename: threading.Lock() for _key, filename, _label, _required in AUDIO_SEPARATOR_MODEL_FILES}

    _audio_separator_float_patch_applied = False

    @classmethod
    def _ensure_audio_separator_float_output(cls):
        """Every audio-separator model (vocal ensemble members, DrumSep
        kick/snare, denoise/de-reverb restoration) computes in float32
        internally, but CommonSeparator's two write paths both throw that
        away: the default (pydub) path hardcodes int16, and so does its own
        'use_soundfile=True' alternative -- its manual stereo-interleave
        branch hardcodes dtype=np.int16 whenever the array isn't already
        Fortran-contiguous, which is true for almost every freshly computed
        array. So using use_soundfile=True alone does NOT avoid the
        quantization; this patches write_audio_soundfile itself to keep
        float32 (soundfile accepts a plain (frames, channels) array directly,
        no manual interleaving needed) while still applying the same
        peak-normalization its pydub counterpart does, so loudness matches
        what every other stem would've gotten either way. Call sites must
        also pass use_soundfile=True to Separator(...) so this path is
        actually used instead of the pydub one. Idempotent -- only patches
        once per process.

        Past known issue (fixed): the original write_audio_soundfile also
        never joins stem_path with self.output_dir -- get_stem_output_path()
        always returns a bare filename (os.path.join() on a single argument
        is a no-op), and only write_audio_pydub ever joined it with
        output_dir itself. Routing through write_audio_soundfile without
        also doing that join silently wrote every stem into the process's
        current working directory instead of output_dir, while this app's
        own code still looked for it under output_dir -- caught via a real
        A/B test run (a 'System error' opening a file that was supposedly
        just written). Fixed below by replicating write_audio_pydub's join.
        """
        if cls._audio_separator_float_patch_applied:
            return
        import os
        from audio_separator.separator.common_separator import CommonSeparator
        from audio_separator.separator.uvr_lib_v5 import spec_utils
        import numpy as np
        import soundfile as sf

        def _write_audio_soundfile_float32(self, stem_path, stem_source):
            stem_source = spec_utils.normalize(wave=stem_source, max_peak=self.normalization_threshold,
                                                min_peak=self.amplification_threshold)
            if np.max(np.abs(stem_source)) < 1e-6:
                self.logger.warning("Warning: stem_source array is near-silent or empty.")
                return
            if self.output_dir:
                os.makedirs(self.output_dir, exist_ok=True)
                stem_path = os.path.join(self.output_dir, stem_path)
            try:
                sf.write(stem_path, np.ascontiguousarray(stem_source), self.sample_rate, subtype='FLOAT')
            except Exception as e:
                self.logger.error(f"Error exporting audio file: {e}")

        CommonSeparator.write_audio_soundfile = _write_audio_soundfile_float32
        cls._audio_separator_float_patch_applied = True

    @classmethod
    def all_startup_items(cls):
        """The full set of startup checks/models, all in "queued" status --
        for a caller (server.py) to populate its state with up front, so a
        GUI can show the complete list immediately instead of items only
        appearing one at a time as check_environment()/prefetch_models()
        actually get to them."""
        items = []
        for name, required in cls.ENVIRONMENT_CHECK_NAMES:
            items.append({"key": f"env_{name}", "label": name, "status": "queued", "required": required})
        for key, _filename, label, required in cls.AUDIO_SEPARATOR_MODEL_FILES:
            items.append({"key": key, "label": label, "status": "queued", "required": required})
        for key, _model_name, label, required in cls.DEMUCS_MODELS:
            items.append({"key": key, "label": label, "status": "queued", "required": required})
        return items

    @staticmethod
    def _is_demucs_model_cached(model_name):
        """Demucs weights live in HuggingFace Hub's cache, under a
        content-hashed 'blobs' dir per repo -- checking for at least one
        blob is a reasonable proxy for "already downloaded" without
        needing a network call to confirm."""
        repo = {"htdemucs": "HTDemucs", "htdemucs_6s": "HTDemucs-6s", "hdemucs_mmi": "Demucs-hdemucs_mmi"}.get(model_name)
        if not repo:
            return False
        blobs_dir = Path.home() / ".cache" / "huggingface" / "hub" / f"models--adefossez--{repo}" / "blobs"
        return blobs_dir.is_dir() and any(blobs_dir.iterdir())

    @classmethod
    def prefetch_models(cls, progress_callback=None):
        """Download every model this app needs, if not already cached, so
        the first real separation a user runs doesn't stall (or, worse,
        race -- see _model_load_locks) on a multi-GB download. Meant to be
        called once at server startup, before any request can trigger a
        real separation. All 7 models are fetched in parallel (one thread
        each) -- they're independent files/URLs, so nothing about them
        requires serializing, and this is what actually gets the download
        time down instead of just displaying it more nicely.

        progress_callback, if given, is called for each model -- first with
        status "checking" (so a caller pacing this for a UI, e.g. server.py,
        has a natural moment to pause before the result), then with the
        result: "cached" (already present, nothing to do), "downloading" +
        eventually "done", or "error" (see "detail" for the exception
        message). Checking for "already cached" first (rather than always
        calling the download-if-needed function) is what lets the GUI show
        an instant checkmark for models that don't need fetching, instead
        of every model looking identical mid-startup. progress_callback is
        called concurrently from multiple threads (one per model) -- it
        must be safe for that (server.py's is, via its state lock).

        Byte-level "progress" (0.0-1.0) is included alongside "downloading"
        for the 5 audio-separator models, by transparently swapping in for
        the exact tqdm progress bar audio-separator's own download code
        already creates for the terminal -- so a GUI caller can show the
        identical progress a terminal would, without this needing to know
        anything about audio-separator's URL-resolution/download internals.
        Since multiple models now download at once, that swap-in is a
        single one installed once for the whole batch (not per-model,
        which would race threads against each other rewriting the same
        module attribute); each thread's own bridge instance instead reads
        which model IT is fetching from thread-local storage, set right
        before that thread's download call.

        Never raises -- a single model failing to download is reported via
        its own "error" status so the rest can still proceed; the caller
        decides what to do with a failed required model."""
        import logging
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        from audio_separator.separator import Separator
        import audio_separator.separator.separator as _as_module

        def report(key, label, status, required, detail=None, progress=None):
            if progress_callback:
                progress_callback({"key": key, "label": label, "status": status,
                                    "required": required, "detail": detail, "progress": progress})
            logging.info(f"🔧 {label}: {status}" + (f" ({progress:.0%})" if progress is not None else ""))

        _tqdm_thread_ctx = threading.local()

        class _Bridge:
            # audio-separator's own download loop calls update() once per
            # 8KB chunk -- for a ~900MB model that's well over 100,000
            # calls. Without throttling, every single one would update
            # shared state, flooding both the console and
            # /api/startup-status for no visible benefit (a GUI polling
            # every ~700ms only ever sees the latest value anyway).
            # Reporting at most ~10x/second is still smooth for a progress
            # bar and cuts that by ~4-5 orders of magnitude.
            _MIN_REPORT_INTERVAL_SEC = 0.1

            def __init__(self, total=0, *_args, **_kwargs):
                self._total = total or 0
                self._n = 0
                self._last_report_time = 0.0
                # Captured at construction time, on whichever thread is
                # actually making this particular download call -- see the
                # docstring above for why this has to be thread-local
                # rather than a closure over one shared key/label/required.
                self._ctx = getattr(_tqdm_thread_ctx, "value", None)

            def update(self, n):
                self._n += n
                if not self._total or not self._ctx:
                    return
                key, label, required = self._ctx
                now = time.monotonic()
                is_last_chunk = self._n >= self._total
                if is_last_chunk or (now - self._last_report_time) >= self._MIN_REPORT_INTERVAL_SEC:
                    self._last_report_time = now
                    report(key, label, "downloading", required, progress=min(self._n / self._total, 1.0))

            def close(self):
                pass

        def _fetch_audio_separator_model(key, filename, label, required):
            report(key, label, "checking", required)
            model_path = AUDIO_SEPARATOR_MODEL_DIR / filename
            if model_path.is_file():
                expected_size = cls.AUDIO_SEPARATOR_MODEL_SIZES.get(filename)
                actual_size = model_path.stat().st_size
                if expected_size is None or actual_size == expected_size:
                    report(key, label, "cached", required)
                    return
                # Present but the wrong size -- almost certainly a download
                # that got killed mid-transfer on a previous run (audio-
                # separator writes straight to this path, no atomic rename).
                # Remove it and fall through to re-download rather than
                # silently handing a broken checkpoint to real separation
                # later, where it'd surface as a much more confusing failure.
                logging.warning(f"{filename} is {actual_size} bytes, expected {expected_size} -- "
                                 "treating as a corrupted/incomplete download and re-fetching")
                try:
                    model_path.unlink()
                except OSError:
                    pass
            report(key, label, "downloading", required, progress=0.0)
            _tqdm_thread_ctx.value = (key, label, required)
            try:
                with cls._model_load_locks[filename]:
                    separator = Separator(output_dir=str(BASE_DIR), model_file_dir=str(AUDIO_SEPARATOR_MODEL_DIR))
                    separator.download_model_files(filename)
                report(key, label, "done", required)
            except Exception as e:
                logging.error(f"Failed to fetch model {filename}: {e}")
                report(key, label, "error", required, detail=str(e))
            finally:
                _tqdm_thread_ctx.value = None

        def _fetch_demucs_model(key, model_name, label, required):
            # Demucs weights come from HuggingFace Hub (~/.cache/huggingface),
            # not audio-separator's own cache -- also persistent, not /tmp,
            # so no equivalent to the AUDIO_SEPARATOR_MODEL_DIR override is
            # needed here. get_model() fetches (if needed) and loads the
            # checkpoint without running inference, which is all we need.
            import demucs.pretrained
            report(key, label, "checking", required)
            if cls._is_demucs_model_cached(model_name):
                report(key, label, "cached", required)
                return
            report(key, label, "downloading", required)
            try:
                demucs.pretrained.get_model(model_name)
                report(key, label, "done", required)
            except Exception as e:
                logging.error(f"Failed to fetch Demucs model {model_name}: {e}")
                report(key, label, "error", required, detail=str(e))

        tasks = [(_fetch_audio_separator_model, args) for args in cls.AUDIO_SEPARATOR_MODEL_FILES]
        tasks += [(_fetch_demucs_model, args) for args in cls.DEMUCS_MODELS]

        original_tqdm = _as_module.tqdm
        _as_module.tqdm = _Bridge
        try:
            with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
                futures = [executor.submit(fn, *args) for fn, args in tasks]
                for f in futures:
                    f.result()  # each task already catches its own exceptions; this just re-raises anything that somehow escaped
        finally:
            _as_module.tqdm = original_tqdm

        logging.info("✅ Model prefetch complete.")

    @classmethod
    def check_environment(cls, progress_callback=None):
        """Check the external tools/hardware this app depends on, so
        problems (missing rubberband, no GPU, etc.) surface as one clear
        report at startup instead of as a confusing failure deep inside
        whatever feature happens to need that dependency first.

        progress_callback, if given, is called twice per check: first with
        status "checking" (a natural pacing point for a UI caller, same as
        prefetch_models), then with the resolved "done"/"error". A failed
        OPTIONAL check also carries "warning_label" -- a short phrase
        ("slow processing", "align/snap disabled") for a UI to show instead
        of a bare, alarming "error" for something that isn't actually
        broken, just a reduced-functionality trade-off. Required checks
        have no warning_label; their failure IS a real error.

        Returns a list of {name, ok, detail, required} dicts (unchanged
        shape from before progress_callback existed, for /api/health's
        sake); nothing here raises."""
        checks = []

        def add(name, ok, detail, required=False, warning_label=None):
            if progress_callback:
                progress_callback({"key": f"env_{name}", "label": name, "status": "checking", "required": required})
            checks.append({"name": name, "ok": bool(ok), "detail": detail, "required": required})
            if progress_callback:
                progress_callback({"key": f"env_{name}", "label": name,
                                    "status": "done" if ok else "error",
                                    "required": required, "detail": detail,
                                    "warning_label": None if (ok or required) else warning_label})

        add("ffmpeg", shutil.which("ffmpeg") is not None,
            "Required for all audio conversion/mixing -- app will not function without it.",
            required=True)

        demucs_ok = shutil.which("demucs") is not None
        if not demucs_ok:
            try:
                probe = subprocess.run([sys.executable, "-m", "demucs", "--help"],
                                       capture_output=True, text=True, timeout=30)
                demucs_ok = probe.returncode == 0
            except Exception:
                demucs_ok = False
        add("demucs", demucs_ok, "Required for stem separation (both legacy and multi-engine mode).",
            required=True)

        add("rubberband", shutil.which("rubberband") is not None,
            "Required for the 'Align beatgrid' and 'Snap to reference' features only -- "
            "the rest of the app works without it.",
            warning_label="align/snap disabled")

        try:
            import torch
            add("gpu", torch.cuda.is_available(),
                "No GPU detected -- separation will still work but run much slower on CPU.",
                warning_label="slow processing")
        except Exception as e:
            add("gpu", False, f"Could not check (torch import failed: {e})", warning_label="slow processing")

        try:
            import audio_separator  # noqa: F401
            add("audio_separator", True, "Multi-engine (9-stem) mode is available.")
        except Exception as e:
            add("audio_separator", False, f"Multi-engine mode unavailable: {e}",
                warning_label="legacy mode only")

        return checks

    def __init__(self):
        self.ffmpeg = "ffmpeg"
        self.stems_dir = BASE_DIR / "separated_stems"

    def separate_stems(self, songs, use_multi_engine=False, progress_callback=None):
        """Stem separation with optional multi-engine mode.

        Args:
            songs: List of audio file paths
            use_multi_engine: If True, use advanced vocal model + Demucs-6s + DrumSep
                             pipeline (9 stems). If False, use legacy Demucs-only
                             (7 stems) for backward compatibility. Denoise + de-reverb
                             restoration is never applied here -- it's opt-in per stem,
                             after separation, via /api/restore-stem.
            progress_callback: Optional callable(fraction, label) invoked at each internal
                             stage boundary. fraction is 0.0-1.0 across all `songs` combined;
                             label is a short human-readable description of the stage just
                             starting. Lets a caller surface real-time progress instead of a
                             single stall for the whole (multi-minute) separation call.

        Returns:
            List of dicts mapping stem names to file paths.
        """
        import logging

        if use_multi_engine:
            return self.separate_stems_multi_engine(songs, progress_callback=progress_callback)

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
        song_count = len(songs)
        for song_idx, song in enumerate(songs):
            def report(local_frac, label):
                if progress_callback:
                    progress_callback((song_idx + local_frac) / song_count, label)

            report(0.0, "Running Demucs separation...")
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

            report(0.85, "Splitting drums (kick/snare/hihat/tom)...")
            try:
                logging.info(f"🥁 Splitting drums for {Path(song).name}...")
                drum_splits = self.split_drums(stems['drums'], str(folder))
                del stems['drums']
                stems.update(drum_splits)
                logging.info("✅ Drums split into: kick, snare, hi-hat, tom")
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
            report(1.0, "Stems ready")
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
                *self.WAV_CODEC_ARGS, padded_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                logging.warning(f"Could not pad {name} to {target:.3f}s: {result.stderr.strip()[-300:]}")
                continue
            shutil.move(padded_path, path)
            logging.info(f"🩹 Padded {name}: {durations[name]:.3f}s → {target:.3f}s")

    def _detect_kick_candidates(self, y, sr, max_search_sec=None, max_candidates=3):
        """Locate up to `max_candidates` genuine kick-drum transients in
        `y`, in chronological order, so beatgrid phase detection can skip a
        vague/drum-less intro instead of anchoring to whatever weak,
        non-percussive content sits at the very start of the track.

        Returns a list of (offset_sec, run_length) tuples in chronological
        order, where run_length is how many qualifying onsets were grouped
        into that candidate's run before the next big gap -- an isolated
        one-off transient has run_length 1, while the start of a real
        repeating groove has run_length > 1. _detect_beats_essentia uses
        run_length to disambiguate cases where an early false lead (e.g. a
        sub-bass drop) and the track's real downbeat score near-identical
        beat-tracking confidence, which happens because both candidates'
        analysis windows share almost all the same downstream audio.

        Kick drums concentrate their energy below ~150 Hz, so this
        low-pass filters the search window first (a plain onset-strength
        pass on the full-band signal would just as happily fire on a
        vocal consonant, a cymbal swell, or a synth pad in the intro).

        Only searches the first `max_search_sec` of `y` -- if the intro
        runs longer than that, or has no clear low-end transient at all,
        returns an empty list and callers fall back to treating the track
        as starting at 0 (the previous behavior).
        """
        import logging
        import numpy as np
        import librosa
        from scipy.signal import butter, sosfiltfilt

        if max_search_sec is None:
            max_search_sec = self.KICK_SEARCH_MAX_SEC

        search_samples = int(max_search_sec * sr)
        y_search = y[:search_samples] if len(y) > search_samples else y
        if len(y_search) < sr:
            return []  # too little audio to search meaningfully

        try:
            sos = butter(4, 150, btype='low', fs=sr, output='sos')
            y_low = sosfiltfilt(sos, y_search)
            onset_env = librosa.onset.onset_strength(y=y_low, sr=sr)
            if not np.any(onset_env):
                return []
            # backtrack=False here: onset_detect's peak positions carry
            # their own onset_env strength, which is what "strong" below
            # filters on. backtrack=True would shift each one to its
            # preceding local minimum instead, where onset_env is ~0 by
            # definition -- that shift is only useful once we've already
            # decided a transient is real and want its exact attack point.
            onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, backtrack=False)
        except Exception as e:
            logging.warning(f"Kick-transient detection failed: {e}")
            return []

        if len(onsets) == 0:
            return []

        onset_times = librosa.frames_to_time(onsets, sr=sr)

        # onset_strength flags the very first frame or two as a spurious
        # "attack" on almost any signal, since there's no preceding audio
        # for it to compare against (an STFT boundary artifact, not a real
        # transient). A genuine kick within that sliver of a track needs no
        # correction anyway -- analysis already starts at ~0 -- so it's
        # safe to just ignore this window entirely.
        boundary_guard = onset_times >= 0.2

        # Validate each candidate by crest factor (peak / RMS) on the raw
        # low-passed waveform around it, rather than trusting onset_env's
        # own scale. onset_env is a spectral-flux measure -- it can be
        # thrown off by a loud/overdriven sub-bass drone in the intro: hard
        # clipping's harmonic buzz can itself register as a string of
        # "onsets" whose relative strength swamps or hides a real kick,
        # depending on how it happens to skew the envelope's baseline.
        # Crest factor sidesteps that: a genuine kick's energy is a short
        # spike far above its surroundings (crest factor in the double
        # digits, measured below), while a sustained drone or pad -- however
        # loud or distorted -- keeps its peak and RMS close together
        # (crest factor near 1-2), because it has no isolated impact to spike.
        half_win = int(self.KICK_CREST_WINDOW_SEC * sr)
        crest_ok = np.zeros(len(onsets), dtype=bool)
        for i, onset_frame in enumerate(onsets):
            center = librosa.frames_to_samples(onset_frame)
            start, end = max(0, center - half_win), min(len(y_low), center + half_win)
            segment = y_low[start:end]
            if len(segment) == 0:
                continue
            peak = np.max(np.abs(segment))
            rms = np.sqrt(np.mean(segment.astype(np.float64) ** 2)) + 1e-9
            crest_ok[i] = (peak / rms) >= self.KICK_CREST_FACTOR_MIN

        candidates = boundary_guard & crest_ok
        if not np.any(candidates):
            return []

        # Group consecutive qualifying onsets into "runs" (gap between them
        # < KICK_CANDIDATE_MIN_GAP_SEC keeps them in the same run), keeping
        # each run's first onset time and how many onsets it contains. A
        # normal song with drums from the start is one continuous run --
        # one candidate, not three consecutive beats of the same groove. A
        # genuine one-off transient followed by a real gap before the
        # actual groove starts becomes two runs (the first of length 1).
        runs = []  # [[offset, run_length], ...]
        last_onset_time = None
        for t in onset_times[candidates]:
            starts_new_run = last_onset_time is None or t - last_onset_time >= self.KICK_CANDIDATE_MIN_GAP_SEC
            if starts_new_run:
                runs.append([float(t), 1])
            else:
                runs[-1][1] += 1
            last_onset_time = t

        return [(offset, run_length) for offset, run_length in runs[:max_candidates]]

    def _pick_kick_offset(self, song_path, kick_candidates, offsets, total_duration):
        """Pick which kick candidate to use as the Librosa fallback's first
        sample-window start, when _detect_kick_candidates found more than one.

        Primarily prefers the candidate with the longest run_length (see
        _detect_kick_candidates) -- the same signal used to disambiguate an
        isolated one-off transient from a real repeating groove on the
        Essentia path. An earlier version of this method instead compared
        each candidate's own librosa.beat.beat_track tempo estimate against
        a reference BPM built from the other sample offsets, on the theory
        that librosa has no confidence/run_length equivalent of its own to
        fall back on. Tested against a sub-drop-then-real-groove fixture,
        that comparison didn't discriminate at all: beat_track's tempo
        estimate is -- by design -- robust to exactly where a mostly
        periodic window starts, so the isolated pre-groove transient and
        the track's real groove start produced the IDENTICAL tempo (measured:
        120.185 BPM for both, and for the reference offsets too). With an
        exact tie, "closest wins" just silently kept the first (wrong)
        candidate -- no improvement over not checking at all. run_length has
        no such issue: it comes from the same crest-validated onset analysis
        regardless of which engine ends up doing the beat tracking, and
        directly measures "is this immediately followed by more of the same
        pattern", which is exactly what separates the two cases.

        BPM agreement (the original idea) is kept only as a tie-break
        between candidates that end up with an equal run_length.

        Returns a start offset in seconds (pre-roll already applied).
        """
        import logging
        import numpy as np
        import librosa

        if len(kick_candidates) == 1:
            return max(0.0, kick_candidates[0][0] - self.KICK_PRE_ROLL_SEC)

        max_run_length = max(run_length for _offset, run_length in kick_candidates)
        top_candidates = [c for c in kick_candidates if c[1] == max_run_length]
        if len(top_candidates) == 1 or len(offsets) < 2:
            return max(0.0, top_candidates[0][0] - self.KICK_PRE_ROLL_SEC)

        def sample_tempo(offset):
            offset = max(0.0, min(offset, total_duration - 10.0))
            window = min(40.0, total_duration - offset)
            y, sr = librosa.load(song_path, sr=None, mono=True, offset=offset, duration=window)
            tempo, _beat_frames = librosa.beat.beat_track(y=y, sr=sr)
            return float(np.asarray(tempo).reshape(-1)[0])

        reference_samples = []
        for offset in offsets[1:]:
            try:
                reference_samples.append(sample_tempo(offset))
            except Exception as e:
                logging.warning(f"  Reference sample @ {offset:.0f}s failed: {e}")

        if not reference_samples:
            return max(0.0, top_candidates[0][0] - self.KICK_PRE_ROLL_SEC)
        reference_bpm = float(np.median(reference_samples))

        best_offset, best_distance = None, None
        for kick_offset, _run_length in top_candidates:
            trial_offset = max(0.0, kick_offset - self.KICK_PRE_ROLL_SEC)
            try:
                tempo = sample_tempo(trial_offset)
            except Exception as e:
                logging.warning(f"  Kick candidate @ {kick_offset:.2f}s failed: {e}")
                continue
            distance = abs(tempo - reference_bpm)
            logging.info(f"  Kick candidate @ {kick_offset:.2f}s (run length {max_run_length}): "
                         f"{tempo:.1f} BPM (reference {reference_bpm:.1f} BPM, distance {distance:.1f})")
            if best_distance is None or distance < best_distance:
                best_distance, best_offset = distance, trial_offset

        if best_offset is not None:
            return best_offset
        return max(0.0, top_candidates[0][0] - self.KICK_PRE_ROLL_SEC)

    def _detect_beats_essentia(self, song_path, min_bpm=40, max_bpm=208):
        """Detect BPM and every individual beat position using Essentia's
        RhythmExtractor2013 (combines multiple beat-tracking algorithms;
        madmom would have been the neural-net alternative here, but it
        doesn't even install in this project's environment -- see
        requirements.txt).

        Before running the extractor, looks for up to a few candidate
        kick-drum transients (see _detect_kick_candidates) and trims the
        vague/drum-less intro off the front of the analysis at the best one,
        so the extractor's beat phase isn't anchored to that quiet section.

        Candidates are ranked by run_length FIRST (how many onsets repeat
        right after the candidate -- see _detect_kick_candidates), with the
        extractor's own confidence used only to break an exact tie in
        run_length. An earlier version did the reverse (confidence primary,
        run_length only breaking close confidence ties), which real-track
        validation (validate_pipeline.py, 38 tracks) showed fails badly:
        confidence mostly reflects how periodic the BULK of the analyzed
        audio is, which barely differs between candidates sharing the same
        downstream track, so it isn't a reliable ranking signal on its own.
        One case made this unambiguous: a candidate with run_length 85 (an
        overwhelmingly dominant, sustained groove) lost to one with
        run_length 4 purely on a confidence difference. run_length, however
        it doesn't rely on anything from the (expensive) extractor at all --
        it comes straight from the crest-validated onset analysis, so
        candidates that aren't tied for the top run_length are never even
        run through the extractor, which also cuts typical-case cost
        (usually to a single Essentia call instead of up to 3).

        The winning trim offset is added back onto every returned tick
        afterward, so `ticks`/`beat_anchor` stay in the original track's
        timeline -- the rest of the app (rendering, alignment, snapping)
        never has to know the intro was skipped for analysis. Falls back to
        analyzing the untrimmed track if no candidate qualifies or all of
        the top-ranked ones fail.

        Returns (bpm, beat_anchor, ticks) where `ticks` is every detected
        beat timestamp in seconds (needed for per-beat beatgrid alignment,
        not just the single average BPM analyze_track() used to return).
        Raises on failure -- callers fall back to Librosa.
        """
        import logging
        from essentia.standard import MonoLoader, RhythmExtractor2013

        sr = 44100  # MonoLoader's default sampleRate
        loader = MonoLoader(filename=str(song_path))
        audio = loader()
        rhythm = RhythmExtractor2013(method='multifeature', minTempo=int(min_bpm), maxTempo=int(max_bpm))

        kick_candidates = []
        try:
            kick_candidates = self._detect_kick_candidates(audio, sr)
        except Exception as e:
            logging.warning(f"Kick-transient pre-detection failed, using full track: {e}")

        # Only the candidate(s) tied for the highest run_length are worth
        # actually running through the extractor -- see the docstring above
        # for why run_length ranks ahead of confidence. Ties are rare (two
        # candidates with the exact same onset count), so this is usually
        # just one candidate.
        if kick_candidates:
            max_run_length = max(run_length for _offset, run_length in kick_candidates)
            kick_candidates = [c for c in kick_candidates if c[1] == max_run_length]

        best = None  # (confidence, run_length, trim_start, bpm, ticks)
        for kick_offset, run_length in kick_candidates:
            # Don't cut exactly on the transient -- a beat tracker's first
            # analyzed frame has no quiet lead-in to compute its own
            # onset/spectral-flux value against, which can bias that very
            # first detected beat (used below as beat_anchor).
            trim_start = max(0.0, kick_offset - self.KICK_PRE_ROLL_SEC)
            # Leave at least 10s of audio after the trim -- an intro that
            # eats almost the whole (short) track leaves nothing for the
            # extractor to actually find a tempo in.
            if len(audio) - int(trim_start * sr) < sr * 10:
                continue

            try:
                bpm, ticks, confidence, _estimates, _bpm_intervals = rhythm(audio[int(trim_start * sr):])
            except Exception as e:
                logging.warning(f"Kick candidate @ {kick_offset:.2f}s failed: {e}")
                continue
            if len(ticks) < 2:
                continue

            # Now that this candidate's rough BPM is known, slow tracks get
            # the pre-roll refined to match their actual beat duration
            # instead of the fixed default. Gated to genuinely slow tracks
            # (see KICK_PRE_ROLL_REFINE_MAX_BPM) rather than "whenever it
            # would change the value" -- 60/bpm diverges from the 0.35s
            # default for most real music, not just outliers, so refining
            # unconditionally would double the Essentia cost on most songs
            # for a benefit that's clearest specifically at slow tempo.
            if 0 < bpm < self.KICK_PRE_ROLL_REFINE_MAX_BPM:
                dynamic_pre_roll = max(self.KICK_PRE_ROLL_MIN_SEC, min(self.KICK_PRE_ROLL_MAX_SEC, 60.0 / bpm))
                refined_trim_start = max(0.0, kick_offset - dynamic_pre_roll)
                if len(audio) - int(refined_trim_start * sr) >= sr * 10:
                    try:
                        r_bpm, r_ticks, r_confidence, _e, _bi = rhythm(audio[int(refined_trim_start * sr):])
                        if len(r_ticks) >= 2:
                            logging.info(f"  Refined @ {kick_offset:.2f}s with tempo-adaptive pre-roll "
                                         f"{dynamic_pre_roll:.2f}s (for ~{bpm:.0f} BPM, vs default "
                                         f"{self.KICK_PRE_ROLL_SEC:.2f}s): {r_bpm:.1f} BPM, "
                                         f"confidence {r_confidence:.2f}")
                            trim_start, bpm, ticks, confidence = refined_trim_start, r_bpm, r_ticks, r_confidence
                    except Exception as e:
                        logging.warning(f"  Pre-roll refinement @ {kick_offset:.2f}s failed, "
                                       f"keeping default: {e}")

            logging.info(f"🥁 Kick candidate @ {kick_offset:.2f}s (run length {run_length}, "
                         f"trim from {trim_start:.2f}s): {bpm:.1f} BPM, confidence {confidence:.2f}")

            # All remaining candidates already share the same (top)
            # run_length -- that filtering happened before this loop -- so
            # confidence only needs to break ties among them here.
            if best is None or confidence > best[0]:
                best = (confidence, run_length, trim_start, bpm, list(ticks))

        if best is not None:
            confidence, run_length, trim_start, bpm, ticks = best
            ticks = [float(t) + trim_start for t in ticks]
            beat_anchor = float(ticks[0])
            quality = "good" if confidence >= self.KICK_CONFIDENCE_GOOD else "low"
            logging.info(f"✅ Essentia BPM: {bpm:.1f}, beat anchor: {beat_anchor:.2f}s, "
                         f"beats: {len(ticks)}, confidence: {confidence:.2f} ({quality}) "
                         f"(kick-trimmed from {trim_start:.2f}s)")
            return float(bpm), beat_anchor, ticks

        # No kick candidate qualified (or all of them failed) -- analyze
        # the untrimmed track, same as before this feature existed.
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

        # If the earliest sample point is a vague/drum-less intro, push it
        # forward to a real kick transient instead -- otherwise beat_track's
        # phase estimate for that pass gets anchored to whatever weak
        # content happens to sit at the window's start.
        try:
            search_window = min(self.KICK_SEARCH_MAX_SEC, total_duration)
            y_search, sr_search = librosa.load(song_path, sr=None, mono=True, duration=search_window)
            kick_candidates = self._detect_kick_candidates(y_search, sr_search)
        except Exception as e:
            kick_candidates = []
            logging.warning(f"Kick-transient pre-detection failed: {e}")

        if kick_candidates:
            offsets[0] = max(offsets[0], self._pick_kick_offset(
                song_path, kick_candidates, offsets, total_duration))

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

                # Push the earliest pass forward to a real kick transient if
                # that pass would otherwise land in a vague/drum-less intro
                # -- same reasoning as analyze_track().
                try:
                    search_samples = int(min(self.KICK_SEARCH_MAX_SEC, total_duration) * sr)
                    kick_candidates = self._detect_kick_candidates(y[:search_samples], sr)
                except Exception as e:
                    kick_candidates = []
                    logging.warning(f"Kick-transient pre-detection failed: {e}")

                if kick_candidates:
                    offsets[0] = max(offsets[0], self._pick_kick_offset(
                        song_path, kick_candidates, offsets, total_duration))

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
                ["ffmpeg", "-y", "-i", str(input_path), "-ar", "44100", *self.WAV_CODEC_ARGS, str(wav_in)],
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
                ["ffmpeg", "-y", "-i", str(input_path), "-ar", "44100", *self.WAV_CODEC_ARGS, str(wav_in)],
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
        grid of `target_bpm`, anchored so ticks[0] lands on the grid point
        closest to its own real position (see the k0 computation below --
        NOT necessarily `target_anchor` itself, since ticks[0] can now sit
        far into the file when kick-trimmed detection skipped a long
        vague/drum-less intro), then warps wav_in through it and encodes the
        result to output_path.

        Returns (time_ratio, mean_correction_ms, max_correction_ms):
        time_ratio is the overall ratio applied (output duration / input
        duration); the correction stats are how far each detected beat sat
        from its ideal grid position BEFORE warping -- i.e. the actual size
        of the drift/misalignment this step corrected, in milliseconds,
        averaged and worst-case across all beats. Past bug: enumerating grid
        targets as target_anchor + i*ideal_interval assumed ticks[0] was at
        i=0 (essentially the start of the track); once kick-trimming could
        put ticks[0] tens of seconds in, that produced a nonsensical tens-
        of-seconds "correction" instead of the intended small drift figure,
        and would have asked rubberband to crush that whole span down to
        nothing. Fixed via k0 below."""
        import subprocess
        import soundfile as sf

        info = sf.info(str(wav_in))
        sr = info.samplerate
        total_frames = info.frames

        ideal_interval = 60.0 / target_bpm

        # ticks[0] is not necessarily "the first beat of the track" anymore
        # -- kick-trimmed detection (_detect_beats_essentia) can put it far
        # into the file when it skips a long vague/drum-less intro. Naively
        # enumerating grid targets as target_anchor + i*ideal_interval
        # (i.e. assuming ticks[0] maps to i=0) would then try to warp that
        # real position all the way back to target_anchor -- e.g. crushing
        # 50 seconds of intro into a fraction of a second when snapping to
        # another song whose own anchor sits near 0. Since the target grid
        # is periodic with period ideal_interval, ANY integer offset k0
        # lands on the exact same absolute-time grid (so phase-locking
        # between songs is unaffected) -- so pick the k0 that keeps ticks[0]
        # closest to its OWN actual position, minimizing distortion instead
        # of introducing a spurious multi-second correction across every tick.
        k0 = round((float(ticks[0]) - target_anchor) / ideal_interval)

        # Timemap: (source_frame, target_frame) pairs -- anchor the file
        # start, snap each beat to its ideal grid position, then hold the
        # tail (after the last beat) at a constant offset so it isn't cut.
        timemap = [(0, 0)]
        corrections_ms = []
        for i, t in enumerate(ticks):
            src = int(float(t) * sr)
            tgt = max(0, int((target_anchor + (k0 + i) * ideal_interval) * sr))
            timemap.append((src, tgt))
            corrections_ms.append(abs(src - tgt) / sr * 1000.0)

        last_src = int(float(ticks[-1]) * sr)
        last_tgt = max(0, int((target_anchor + (k0 + len(ticks) - 1) * ideal_interval) * sr))
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
            ["ffmpeg", "-y", "-i", str(wav_out), *self.WAV_CODEC_ARGS, str(output_path)],
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
                ["ffmpeg", "-y", "-i", str(input_path), "-af", f"volume={gain_db:.3f}dB", *self.WAV_CODEC_ARGS, temp_output],
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

    def write_cue_markers(self, wav_path, bpm, beat_anchor=0.0):
        """Append a WAV 'cue ' chunk (+ 'LIST'/'adtl' labels) with one marker
        per beat, evenly spaced at 60/bpm from beat_anchor across the whole
        file. This is the actual convention FL Studio (and Sound Forge, Adobe
        Audition, REX/Recycle-style slicers, ...) read as visible markers on
        import -- distinct from the ACID chunk above, which only lets a DAW
        *compute* a grid from tempo/beat-count rather than showing one.
        Mutates wav_path in place. Assumes a single uncompressed 'data' chunk
        (true for every WAV this app produces).
        """
        import struct
        import soundfile as sf

        with open(wav_path, 'rb') as f:
            data = f.read()
        if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
            raise ValueError(f"{wav_path} is not a valid WAV file")

        info = sf.info(str(wav_path))
        sample_rate = info.samplerate
        duration = info.duration
        interval = 60.0 / bpm

        # Walk back from the anchor to the start of the file so the very
        # first beat gets a marker too, not just ones at/after beat_anchor.
        t = beat_anchor % interval
        beat_times = []
        while t < duration:
            beat_times.append(t)
            t += interval

        if not beat_times:
            return

        cue_points = b''
        labels = b''
        for i, beat_t in enumerate(beat_times):
            cue_id = i + 1
            sample_pos = int(round(beat_t * sample_rate))
            # dwName, dwPosition, fccChunk, dwChunkStart, dwBlockStart, dwSampleOffset --
            # chunkStart/blockStart are 0 since there's only one 'data' chunk and PCM
            # samples are addressed directly (no compressed-block indirection).
            cue_points += struct.pack('<II4sIII', cue_id, sample_pos, b'data', 0, 0, sample_pos)

            label_text = f"Beat {cue_id}".encode('ascii') + b'\x00'
            labl_payload = struct.pack('<I', cue_id) + label_text
            if len(labl_payload) % 2:
                labl_payload += b'\x00'
            labels += b'labl' + struct.pack('<I', len(labl_payload)) + labl_payload

        cue_payload = struct.pack('<I', len(beat_times)) + cue_points
        cue_chunk = b'cue ' + struct.pack('<I', len(cue_payload)) + cue_payload
        if len(cue_payload) % 2:
            cue_chunk += b'\x00'

        adtl_payload = b'adtl' + labels
        list_chunk = b'LIST' + struct.pack('<I', len(adtl_payload)) + adtl_payload
        if len(adtl_payload) % 2:
            list_chunk += b'\x00'

        new_data = data + cue_chunk + list_chunk
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
                        "-y", *self.WAV_CODEC_ARGS, temp_output
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
                    "-y", *self.WAV_CODEC_ARGS, str(output_path)
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

    def render(self, params):
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
                    "Try reprocessing this song's stems."
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

        # WAV, not a FLAC re-encode of an already-lossy MP3 (what this used
        # to produce) -- genuinely lossless, and it's what write_acid_chunk()
        # needs to embed tempo/key info DAWs can actually read.
        output = str(BASE_DIR / "final_remix.wav")
        command = [self.ffmpeg, "-y", *inputs, "-filter_complex", filter_complex, "-map", "[final]",
                   *self.WAV_CODEC_ARGS, output]

        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except FileNotFoundError as error:
            raise RuntimeError("FFmpeg is not installed or is not in PATH. Install FFmpeg before rendering.") from error

        try:
            stdout, stderr = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()

        if proc.returncode != 0:
            detail = (stderr or "").strip() or "FFmpeg failed without an error message."
            raise RuntimeError(f"FFmpeg could not make the mix:\n{detail[-1200:]}")

        return output

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

            # Export each frequency band to its own file (one FFmpeg call per
            # stem -- simpler than a single combined filter_complex graph,
            # and easier to report a per-stem failure for)
            for name in ('kick', 'snare', 'hihat', 'tom'):
                if name == 'kick':
                    filters = "[0:a]lowpass=f=250,highpass=f=20"
                elif name == 'snare':
                    filters = "[0:a]bandpass=f=4000:width_type=o:width=2"
                elif name == 'hihat':
                    filters = "[0:a]highpass=f=5000"
                elif name == 'tom':
                    filters = "[0:a]bandpass=f=1000:width_type=o:width=1"

                cmd = [
                    self.ffmpeg, "-i", str(drum_stem_path),
                    "-af", filters,
                    "-y", *self.WAV_CODEC_ARGS,
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

    def separate_stems_multi_engine(self, songs, progress_callback=None):
        """Advanced multi-engine stem separation: a vocal-model ensemble (vocals) +
        Demucs htdemucs_6s (guitar/piano/other) + Demucs hdemucs_mmi
        (bass/drums, see _separate_bass_drums_mmi for why) + MDX23C DrumSep
        (kick/snare, ML) + frequency-split (hihat/tom, approximate). Denoise +
        de-reverb restoration is not applied here -- it's opt-in per stem,
        after separation, via /api/restore-stem.

        Every stem is derived straight from the full song: the vocal model and
        both Demucs passes run directly against the original/processed WAV.
        The one deliberate exception is kick/snare/hihat/tom -- both DrumSep
        and the frequency-split fallback need an isolated drum stem, not a
        full mix, so stage 2 runs them on hdemucs_mmi's (refined) 'drums'
        output rather than on the song itself.

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
        song_count = len(songs)
        for song_idx, song in enumerate(songs):
            def report(local_frac, label):
                if progress_callback:
                    progress_callback((song_idx + local_frac) / song_count, label)

            logging.info(f"\n{'='*60}")
            logging.info(f"🎵 Processing Song {song_idx + 1}/{len(songs)}: {Path(song).name}")
            logging.info(f"{'='*60}")

            song_hash = hashlib.sha1(str(Path(song).resolve()).encode("utf-8")).hexdigest()[:16]
            song_out_dir = self.stems_dir / song_hash
            song_out_dir.mkdir(exist_ok=True, parents=True)

            # STAGE 1: Parallel extraction, both straight from the full song
            report(0.0, "Stage 1: extracting vocals + separating instruments...")
            logging.info("📊 [STAGE 1] Parallel vocal + multi-instrument extraction...")

            vocals_path = None
            vocals_bonus = None
            demucs_stems = None

            vocals_lock = threading.Lock()
            demucs_lock = threading.Lock()

            # Demucs is the slower of the two (minutes vs. ~tens of seconds
            # for vocals once its model is cached), so its own real percentage
            # -- parsed straight from its progress bar -- drives the Stage 1
            # progress fraction; vocals only contributes descriptive text,
            # since audio-separator doesn't expose a percentage of its own.
            stage1_lock = threading.Lock()
            stage1_status = {'vocals': 'Vocals: starting...', 'instruments_frac': 0.0,
                              'instruments_label': 'starting...'}

            def _report_stage1():
                with stage1_lock:
                    frac = stage1_status['instruments_frac']
                    combined = (f"Stage 1: {stage1_status['instruments_label']} "
                                f"| {stage1_status['vocals']}")
                report(frac * 0.55, combined)

            def _set_vocals_status(label):
                with stage1_lock:
                    stage1_status['vocals'] = label
                _report_stage1()

            def _set_instruments_status(frac, label):
                with stage1_lock:
                    stage1_status['instruments_frac'] = frac
                    stage1_status['instruments_label'] = label
                _report_stage1()

            def extract_vocals():
                nonlocal vocals_path, vocals_bonus
                try:
                    logging.info("  🎤 Vocal model ensemble: extracting vocals...")
                    with _report_audio_separator_progress("Vocals", _set_vocals_status):
                        v, bonus = self._separate_vocals_karaoke(str(song), str(song_out_dir))
                    _set_vocals_status("Vocals: done")
                    with vocals_lock:
                        vocals_path = v
                        vocals_bonus = bonus
                    logging.info("  ✅ Vocals extracted")
                except Exception as e:
                    logging.error(f"  ❌ Vocal separation failed: {e}")
                    with vocals_lock:
                        vocals_path = None

            def extract_instruments():
                nonlocal demucs_stems
                try:
                    logging.info("  🎼 Demucs htdemucs_6s: extracting bass/guitar/piano/other/drums...")
                    # Two sequential Demucs passes share this thread's 0.0-1.0
                    # progress budget (0.0-0.5 / 0.5-1.0) so the reported
                    # fraction keeps climbing instead of resetting to 0% when
                    # the second pass starts.
                    stems = self._separate_stems_demucs6s(
                        str(song), str(song_out_dir),
                        progress_callback=lambda frac, label: _set_instruments_status(frac * 0.5, label))
                    logging.info("  🎚️ Demucs hdemucs_mmi: refining bass/drums...")
                    refined = self._separate_bass_drums_mmi(
                        str(song), str(song_out_dir),
                        progress_callback=lambda frac, label: _set_instruments_status(0.5 + frac * 0.5, label))
                    stems['bass'] = refined['bass']
                    stems['drums'] = refined['drums']
                    _set_instruments_status(1.0, "extracting bass/guitar/piano/other/drums (Demucs htdemucs_6s + hdemucs_mmi): done")
                    with demucs_lock:
                        demucs_stems = stems
                    logging.info("  ✅ 6-stem separation complete (bass/drums refined via hdemucs_mmi)")
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
            report(0.55, "Stage 2: splitting drums (kick/snare/hihat/tom)...")
            logging.info("🥁 [STAGE 2] Kick/snare (MDX23C DrumSep) + hihat/tom (frequency split)...")
            drums_stem = demucs_stems['drums']
            with _report_audio_separator_progress(
                    "Kick/snare (DrumSep)", lambda label: report(0.6, f"Stage 2: {label}")):
                kick_snare = self._split_drums_mdx23c(drums_stem, str(song_out_dir))
            hihat_tom = self.split_drums(drums_stem, str(song_out_dir))

            # STAGE 3: Assemble final stem structure
            report(0.75, "Stage 3: assembling stems...")
            logging.info("🔧 [STAGE 3] Assembling final stem structure...")
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

            self._conform_stem_lengths(final_stems)

            # Bonus/reference stems generated as byproducts above, never
            # used in the main mix but kept for download rather than thrown
            # away: the ensemble's own two individual models' vocals +
            # instrumental outputs, and Demucs' own (unused) vocals stem.
            # Not conformed to a common length like final_stems above --
            # these are reference material, not meant for sample-aligned
            # mixing. Stashed under a dunder key so callers can pop it off
            # before treating the rest of this dict as real stem paths.
            bonus_stems = dict(vocals_bonus or {})
            demucs_vocals = demucs_stems.get('vocals')
            if demucs_vocals and Path(demucs_vocals).is_file():
                bonus_stems['vocals_demucs'] = demucs_vocals
            final_stems['__bonus__'] = bonus_stems

            results.append(final_stems)
            report(1.0, "Stems ready")
            logging.info(f"✅ Song {song_idx + 1} complete: 9 stems ready\n")

        return results

    # Ensemble of two vocal models (see _separate_vocals_karaoke): each run
    # independently on the full song, then averaged sample-by-sample. A
    # known technique (UVR's "Ensemble Mode") -- different architectures
    # make different mistakes, so averaging smooths those out.
    # (bonus-download slug, model filename) -- the slug names the two bonus
    # "vocals_<slug>"/"instrumental_<slug>" downloads _separate_vocals_karaoke
    # also produces (see below), alongside the averaged ensemble it returns.
    #
    # Past decision (superseded): this ensemble originally paired BS-Roformer
    # with vocals_mel_band_roformer.ckpt (12.60 dB SDR) -- verified by ear
    # 2026-09-27 to beat either model alone. Replaced 2026-10-01: a live A/B/C
    # listening test against a real track (not just SDR numbers, which
    # audio-separator's own registry doesn't even list for this newer
    # checkpoint) showed becruily's Mel-Band RoFormer vocals model beating
    # the old ensemble outright, and ensembling IT with BS-Roformer (instead
    # of the old Mel-Band model) beating becruily alone by a smaller margin
    # -- so becruily replaced vocals_mel_band_roformer.ckpt here, BS-Roformer
    # unchanged.
    _ENSEMBLE_VOCAL_MODELS = [
        ("becruily", "mel_band_roformer_vocals_becruily.ckpt"),
        ("bs_roformer", "model_bs_roformer_ep_368_sdr_12.9628.ckpt"),
    ]

    def _separate_vocals_karaoke(self, audio_path, output_dir):
        """Extract clean lead vocals as the average of two Mel-Band/BS-RoFormer
        vocal models (see _ENSEMBLE_VOCAL_MODELS), each run directly on the
        full song (not on an already-separated stem).

        Each model's own output files are named after the model's
        *filename*, which for mel_band_roformer_vocals_becruily.ckpt happens
        to contain "vocals" itself, so the non-vocals "(other)" file also
        matches a naive 'vocal' in filename check -- matching on the
        parenthesized "(vocals)" stem marker specifically avoids silently
        picking that wrong file.

        Returns (ensemble_path, bonus_stems): the averaged ensemble result
        (what the pipeline actually uses as 'vocals'), plus a dict of the
        individual models' own vocals/instrumental outputs -- already
        computed as a byproduct of the averaging, so kept as bonus
        downloads (see /api/download-stems-zip) instead of thrown away.
        """
        import logging
        import re
        import soundfile as sf

        try:
            from audio_separator.separator import Separator
        except ImportError:
            raise RuntimeError(
                "Multi-engine mode requires: pip install audio-separator onnxruntime\n"
                "Models download automatically on first use."
            )
        MashupEngine._ensure_audio_separator_float_output()

        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(exist_ok=True, parents=True)

        def _vocals_with_model(model_filename):
            separator = Separator(output_dir=str(output_dir_path), output_format="WAV", use_soundfile=True,
                                   model_file_dir=str(AUDIO_SEPARATOR_MODEL_DIR))
            with MashupEngine._model_load_locks[model_filename]:
                separator.load_model(model_filename=model_filename)

            logging.info(f"  Separating vocals from {Path(audio_path).name} using {model_filename}...")
            output_files = separator.separate(audio_path)
            resolved = [p if Path(p).is_absolute() else str(output_dir_path / p) for p in output_files]

            logging.info(f"  {model_filename} produced: {[Path(p).name for p in resolved]}")

            vocals_matches = [p for p in resolved if re.search(r'\(vocals\)', Path(p).stem, re.IGNORECASE)]
            if not vocals_matches:
                raise RuntimeError(
                    f"Could not identify the vocals file among: {[Path(p).name for p in resolved]}"
                )
            # Whichever other output(s) this model produced alongside vocals
            # (its "(other)"/"(Instrumental)" complement) -- there's normally
            # just one, but keep this robust to a model with more than two.
            instrumental_matches = [p for p in resolved if p not in vocals_matches]
            return vocals_matches[0], (instrumental_matches[0] if instrumental_matches else None)

        try:
            bonus_stems = {}
            vocals_paths = []
            for slug, model_filename in self._ENSEMBLE_VOCAL_MODELS:
                vocals_path, instrumental_path = _vocals_with_model(model_filename)
                vocals_paths.append(vocals_path)
                bonus_stems[f"vocals_{slug}"] = vocals_path
                if instrumental_path:
                    bonus_stems[f"instrumental_{slug}"] = instrumental_path

            audio_a, sr_a = sf.read(vocals_paths[0])
            audio_b, sr_b = sf.read(vocals_paths[1])
            if sr_a != sr_b:
                raise RuntimeError(f"Sample rate mismatch between ensemble models: {sr_a} vs {sr_b}")
            n = min(len(audio_a), len(audio_b))
            ensemble = (audio_a[:n] + audio_b[:n]) / 2.0

            ensemble_path = str(output_dir_path / f"{Path(audio_path).stem}_(vocals)_ensemble.wav")
            sf.write(ensemble_path, ensemble, sr_a, subtype='FLOAT')
            return ensemble_path, bonus_stems
        except Exception as e:
            logging.error(f"Vocal separation failed: {e}")
            raise RuntimeError(f"Vocal extraction failed: {str(e)[-500:]}")

    def _run_demucs(self, model_name, audio_path, output_dir, label, progress_callback=None):
        """Run a Demucs model against the full song; return the path to its
        output folder (one .wav per stem the model produces). Shared by
        _separate_stems_demucs6s and _separate_bass_drums_mmi.

        progress_callback(fraction, label), if given, is fed straight from
        Demucs' own tqdm progress bar on stderr (e.g. " 42%|...") -- each of
        these calls is a multi-minute chunk of Stage 1's runtime, so without
        it the bar sits frozen the entire time Demucs is actually working.
        """
        import re
        import threading

        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(exist_ok=True, parents=True)

        command = [sys.executable, "-m", "demucs", "-n", model_name, "--float32",
                   "--out", str(output_dir_path), audio_path]
        percent_re = re.compile(r"(\d+)%\|")

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)

        stdout_lines, stderr_lines = [], []

        def _watch(stream, sink, parse_progress):
            for line in stream:
                sink.append(line)
                if parse_progress and progress_callback:
                    match = percent_re.search(line)
                    if match:
                        progress_callback(int(match.group(1)) / 100.0, label)

        watchers = [
            threading.Thread(target=_watch, args=(process.stdout, stdout_lines, False), daemon=True),
            threading.Thread(target=_watch, args=(process.stderr, stderr_lines, True), daemon=True),
        ]
        for w in watchers:
            w.start()

        try:
            process.wait(timeout=3600)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RuntimeError(f"Demucs {model_name} timed out separating {Path(audio_path).name}")
        for w in watchers:
            w.join(timeout=5)

        if process.returncode != 0:
            detail = "".join(stderr_lines).strip() or "".join(stdout_lines).strip() or "Demucs failed without an error message."
            raise RuntimeError(f"Demucs {model_name} could not separate {Path(audio_path).name}:\n{detail[-1200:]}")

        song_folder = Path(audio_path).stem
        candidates = list(output_dir_path.glob(f"{model_name}/{song_folder}"))
        if not candidates:
            raise RuntimeError(f"Demucs {model_name} finished, but no stem folder was found for {Path(audio_path).name}.")
        return candidates[0]

    def _separate_stems_demucs6s(self, audio_path, output_dir, progress_callback=None):
        """Separate bass/guitar/piano/other/drums directly from the full song using
        Demucs' htdemucs_6s model (also outputs a 'vocals' stem, unused here --
        the dedicated vocal-model ensemble above supplies our (higher-quality) vocals instead).
        bass/drums get further refined by _separate_bass_drums_mmi afterward --
        this call is still needed for guitar/piano/other (and as the source
        for kick/snare/hihat/tom before that refinement, see Stage 2).
        """
        import logging

        folder = self._run_demucs("htdemucs_6s", audio_path, output_dir,
                                   "extracting bass/guitar/piano/other/drums (Demucs htdemucs_6s)...",
                                   progress_callback=progress_callback)

        # 'vocals' is Demucs' own (unused in the main mix -- the ensemble
        # above supplies the real vocals stem) but kept as a bonus/reference
        # download rather than thrown away, since it's already sitting right
        # here for free.
        stems = {name: str(folder / f"{name}.wav") for name in ("drums", "bass", "other", "guitar", "piano", "vocals")}
        missing = [name for name, path in stems.items() if not Path(path).is_file()]
        if missing:
            raise RuntimeError(f"Demucs htdemucs_6s did not create all expected stems: {', '.join(missing)}")

        logging.info(f"  Demucs htdemucs_6s produced: {list(stems.keys())}")
        return stems

    def _separate_bass_drums_mmi(self, audio_path, output_dir, progress_callback=None):
        """Refine bass + drums using Demucs' hdemucs_mmi model instead of
        htdemucs_6s. Verified by ear + registry SDR (2026-09-30):
        htdemucs_6s trades away bass/drums quality for its extra guitar/
        piano split -- it's the worst Demucs variant for both (bass 10.10
        dB, drums 8.47 dB), while hdemucs_mmi scores bass 12.23 dB / drums
        9.64 dB. Since kick/snare/hihat/tom (Stage 2) all derive from the
        drums stem, this improves four downstream stems, not just bass.
        guitar/piano/other stay sourced from htdemucs_6s -- it's the only
        model in the registry that separates those at all.

        Returns {'bass': path, 'drums': path}. This model's own vocals/
        other outputs aren't used (existing sources are equal or better)
        and are left on disk rather than surfaced as bonus stems, unlike
        htdemucs_6s's unused vocals.wav (that one meaningfully differs from
        the ensemble; this model's vocals/other don't add anything new).
        """
        import logging

        folder = self._run_demucs("hdemucs_mmi", audio_path, output_dir,
                                   "refining bass/drums (Demucs hdemucs_mmi)...",
                                   progress_callback=progress_callback)

        stems = {name: str(folder / f"{name}.wav") for name in ("bass", "drums")}
        missing = [name for name, path in stems.items() if not Path(path).is_file()]
        if missing:
            raise RuntimeError(f"Demucs hdemucs_mmi did not create all expected stems: {', '.join(missing)}")

        logging.info(f"  Demucs hdemucs_mmi produced: {list(stems.keys())}")
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
        MashupEngine._ensure_audio_separator_float_output()

        output_dir_path = Path(output_dir)
        output_dir_path.mkdir(exist_ok=True, parents=True)

        try:
            separator = Separator(output_dir=str(output_dir_path), output_format="WAV", use_soundfile=True,
                                   model_file_dir=str(AUDIO_SEPARATOR_MODEL_DIR))
            with MashupEngine._model_load_locks["MDX23C-DrumSep-aufr33-jarredou.ckpt"]:
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
                    "above to match this model's actual naming."
                )
            return mapping
        except Exception as e:
            logging.error(f"MDX23C DrumSep failed: {e}")
            raise RuntimeError(f"Drum splitting failed: {str(e)[-500:]}")

    def _apply_hifi_restoration(self, stems, output_dir, progress_callback=None):
        """Apply ML-based restoration (denoise + de-reverb) to all stems for
        artifact removal.

        This used to call the CPJKU "music-source-restoration" project (a
        HiFi++ GAN). That repo has no setup.py/pyproject.toml (not
        pip-installable at all) and doesn't even contain the module this
        code imported (restoration.mixture_inference / create_mixture_system
        never existed there) -- so that path always raised ImportError and
        silently fell back to spectral filtering, no matter what was
        installed. Replaced with audio-separator's own denoise + de-reverb
        Mel-Band Roformer models: same download/inference infrastructure
        already proven for vocals/drum separation above, with real published
        checkpoints and measured SDR (27.99 dB denoise, 19.17 dB de-reverb).

        Falls back to spectral filtering only if these models can't be
        loaded (e.g. no internet for the first-time checkpoint download).

        progress_callback(fraction, label), if given, is invoked once per
        stem (fraction 0.0-1.0 across all stems) -- this stage processes
        every stem one at a time and can otherwise take minutes with no
        visible movement at all.
        """
        import logging

        try:
            return self._apply_ml_restoration(stems, output_dir, progress_callback=progress_callback)
        except Exception as e:
            logging.warning(f"ML restoration unavailable ({e}), using spectral filtering")
            return self._apply_spectral_restoration(stems, output_dir, progress_callback=progress_callback)

    def _apply_ml_restoration(self, stems, output_dir, progress_callback=None):
        """Denoise, then de-reverb, every stem via audio-separator's
        highest-SDR general-purpose models for each. Not vocal-specific, so
        applying the same two models to every stem (including drums) is
        valid, though drum transients are the least tested case.
        """
        import logging

        try:
            from audio_separator.separator import Separator
        except ImportError:
            raise RuntimeError(
                "ML restoration requires: pip install audio-separator onnxruntime\n"
                "Models download automatically on first use."
            )
        MashupEngine._ensure_audio_separator_float_output()

        restore_dir = Path(output_dir) / "restoration"
        restore_dir.mkdir(exist_ok=True, parents=True)

        # Loaded once and reused across every stem below -- reloading a
        # checkpoint per stem would multiply model-load time (up to ~1 min
        # for the de-reverb model) by however many stems there are.
        with _report_audio_separator_progress(
                "Restoration", lambda label: progress_callback(0.0, label) if progress_callback else None):
            denoiser = Separator(output_dir=str(restore_dir), output_format="WAV", use_soundfile=True,
                                  model_file_dir=str(AUDIO_SEPARATOR_MODEL_DIR))
            dereverber = Separator(output_dir=str(restore_dir), output_format="WAV", use_soundfile=True,
                                    model_file_dir=str(AUDIO_SEPARATOR_MODEL_DIR))
            with MashupEngine._model_load_locks["denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt"]:
                denoiser.load_model(model_filename="denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt")
            with MashupEngine._model_load_locks["dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt"]:
                dereverber.load_model(model_filename="dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt")

        def _resolve(output_files, marker):
            for f in output_files:
                resolved = f if Path(f).is_absolute() else str(restore_dir / f)
                if marker in Path(resolved).stem.lower():
                    return resolved
            return None

        restored = {}
        stem_items = list(stems.items())
        total = len(stem_items)
        for i, (stem_name, stem_path) in enumerate(stem_items):
            if progress_callback:
                progress_callback(i / total, f"Denoise + de-reverb: {stem_name} ({i + 1}/{total})")

            if not stem_path or not Path(stem_path).is_file():
                restored[stem_name] = stem_path
                continue

            try:
                denoised_path = _resolve(denoiser.separate(stem_path), 'dry')
                if not denoised_path:
                    raise RuntimeError("denoise model didn't produce a 'dry' output")

                dereverbed_path = _resolve(dereverber.separate(denoised_path), 'noreverb')
                if not dereverbed_path:
                    raise RuntimeError("de-reverb model didn't produce a 'noreverb' output")

                restored[stem_name] = dereverbed_path
                logging.info(f"    ✓ Denoise + de-reverb restored: {stem_name}")

            except Exception as e:
                logging.warning(f"    ⚠️  ML restoration failed for {stem_name}: {e}")
                restored[stem_name] = stem_path

        if progress_callback:
            progress_callback(1.0, "Denoise + de-reverb complete")
        return restored

    def _apply_spectral_restoration(self, stems, output_dir, progress_callback=None):
        """Fallback spectral restoration (no ML model required).

        Uses FFmpeg spectral filtering to remove shimmer/artifacts.
        Quality lower than the ML denoise/de-reverb models but no model downloads required.
        """
        import logging

        restored = {}
        stem_items = list(stems.items())
        total = len(stem_items)
        for i, (stem_name, stem_path) in enumerate(stem_items):
            if progress_callback:
                progress_callback(i / total, f"Spectral restoration: {stem_name} ({i + 1}/{total})")

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
                    *self.WAV_CODEC_ARGS, restored_path
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

        if progress_callback:
            progress_callback(1.0, "Spectral restoration complete")
        return restored
