#!/usr/bin/env python3
"""
Standalone validation script for the kick-transient detection pipeline
(_detect_kick_candidates / _detect_beats_essentia in mashup_engine.py).

Runs the REAL detection logic (no mocks) against a folder of real audio
files and reports per-track metrics plus an aggregate summary, so the
crest-factor threshold (KICK_CREST_FACTOR_MIN), candidate gap
(KICK_CANDIDATE_MIN_GAP_SEC) and related constants can be sanity-checked
against real, diverse genres instead of only the synthetic fixtures used
during development.

Usage:
    python3 validate_pipeline.py <folder_with_audio_files>

Put test tracks (MP3/WAV/FLAC/M4A/OGG) in Audio/validation_tracks/ -- that
folder is already git-ignored, so real music never ends up committed.
"""
import logging
import re
import sys
import time
from pathlib import Path

from mashup_engine import MashupEngine

SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}

# Parses mashup_engine.py's own INFO log lines rather than reaching into its
# internals -- keeps this script decoupled from _detect_beats_essentia's
# implementation details, at the cost of staying in sync with its log
# wording (see mashup_engine.py's "Kick candidate" / "kick-trimmed from" logs).
KICK_CANDIDATE_LOG_RE = re.compile(
    r"Kick candidate @ ([\d.]+)s \(run length (\d+), trim from ([\d.]+)s\): "
    r"([\d.]+) BPM, confidence ([\d.]+)"
)
FINAL_TRIM_LOG_RE = re.compile(r"kick-trimmed from ([\d.]+)s")


class _CaptureHandler(logging.Handler):
    """Collects log messages emitted during one call, without printing them."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def analyze_one(engine, path):
    """Run kick-candidate detection + full Essentia beat detection on one
    file. Never raises -- any failure is captured in the returned dict so
    one bad file can't abort the whole batch.
    """
    result = {
        "file": path.name,
        "status": "ok",
        "error": None,
        "candidates": [],       # [(offset_sec, run_length), ...] from _detect_kick_candidates
        "essentia_runs": [],    # per-candidate (offset, run_length, trim_start, bpm, confidence)
        "final_trim_start": None,
        "bpm": None,
        "beat_anchor": None,
        "n_ticks": None,
        "elapsed_sec": None,
    }
    t0 = time.monotonic()

    try:
        # Match _detect_beats_essentia's own decoder exactly (Essentia's
        # MonoLoader), not librosa. For glitchy/corrupted MP3s -- common in
        # real-world DJ collections, and visible as "invalid frame, skipping
        # it" warnings throughout this validation set -- the two decoders
        # can disagree on exact sample timing enough to find genuinely
        # different onsets. Loading via librosa here made this script's
        # "candidates" column silently NOT represent what the real pipeline
        # sees for those files (confirmed directly: one such file's
        # librosa-decoded candidates were [(25.41s, run=20), (36.80s,
        # run=29)], but its Essentia-decoded candidates -- what
        # _detect_beats_essentia actually acts on -- were [(25.41s,
        # run=10), (31.87s, run=4)]).
        from essentia.standard import MonoLoader
        loader = MonoLoader(filename=str(path))
        y = loader()
        sr = 44100  # MonoLoader's default sampleRate
    except Exception as e:
        result["status"] = "load_failed"
        result["error"] = str(e)
        result["elapsed_sec"] = time.monotonic() - t0
        return result

    try:
        result["candidates"] = engine._detect_kick_candidates(y, sr)
    except Exception as e:
        result["status"] = "kick_candidates_crashed"
        result["error"] = str(e)

    handler = _CaptureHandler()
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        bpm, beat_anchor, ticks = engine._detect_beats_essentia(str(path))
        result["bpm"] = bpm
        result["beat_anchor"] = beat_anchor
        result["n_ticks"] = len(ticks)
    except Exception as e:
        if result["status"] == "ok":
            result["status"] = "essentia_crashed"
        result["error"] = (result["error"] + " | " if result["error"] else "") + str(e)
    finally:
        root_logger.removeHandler(handler)

    for msg in handler.records:
        m = KICK_CANDIDATE_LOG_RE.search(msg)
        if m:
            result["essentia_runs"].append({
                "offset": float(m.group(1)),
                "run_length": int(m.group(2)),
                "trim_start": float(m.group(3)),
                "bpm": float(m.group(4)),
                "confidence": float(m.group(5)),
            })
        m2 = FINAL_TRIM_LOG_RE.search(msg)
        if m2:
            result["final_trim_start"] = float(m2.group(1))

    if result["status"] == "ok" and not result["candidates"]:
        # Not necessarily wrong -- a track with drums from the very start
        # legitimately has nothing to skip. Flagged so it can be eyeballed.
        result["status"] = "no_candidates"

    result["elapsed_sec"] = time.monotonic() - t0
    return result


def print_summary(results):
    print("\n" + "=" * 88)
    print("SUMMARY")
    print("=" * 88)

    print(f"{'File':<38} {'Status':<22} {'#Cand':>6} {'Trim(s)':>8} {'BPM':>7} {'Time(s)':>8}")
    print("-" * 88)
    for r in results:
        n_cand = len(r["candidates"])
        trim = f"{r['final_trim_start']:.2f}" if r["final_trim_start"] is not None else "-"
        bpm = f"{r['bpm']:.1f}" if r["bpm"] is not None else "-"
        name = r["file"] if len(r["file"]) <= 38 else r["file"][:35] + "..."
        print(f"{name:<38} {r['status']:<22} {n_cand:>6} {trim:>8} {bpm:>7} {r['elapsed_sec']:>8.1f}")

    n = len(results)
    no_cand = [r for r in results if r["status"] == "no_candidates"]
    crashed = [r for r in results if r["status"] in ("kick_candidates_crashed", "essentia_crashed", "load_failed")]
    ok = [r for r in results if r["status"] == "ok"]
    times = [r["elapsed_sec"] for r in results if r["elapsed_sec"] is not None]
    cand_counts = [len(r["candidates"]) for r in results]

    print("-" * 88)
    print(f"\nTracks processed:        {n}")
    print(f"OK (>=1 candidate found): {len(ok)}")
    print(f"No candidates found:      {len(no_cand)}"
          + (f"  -> {', '.join(r['file'] for r in no_cand)}" if no_cand else ""))
    print(f"Crashed / load failed:    {len(crashed)}"
          + (f"  -> {', '.join(r['file'] for r in crashed)}" if crashed else ""))

    if times:
        print(f"\nProcessing time (sec):  min={min(times):.1f}  max={max(times):.1f}  "
              f"avg={sum(times) / len(times):.1f}")
    if cand_counts:
        print(f"Candidates per track:   min={min(cand_counts)}  max={max(cand_counts)}  "
              f"avg={sum(cand_counts) / len(cand_counts):.2f}")

    multi = [r for r in results if len(r["candidates"]) > 1]
    if multi:
        print(f"\nTracks with >1 kick candidate (worth a manual listen -- did the right one win?):")
        for r in multi:
            offsets = ", ".join(f"{o:.2f}s(run={rl})" for o, rl in r["candidates"])
            chosen = f"{r['final_trim_start']:.2f}s" if r["final_trim_start"] is not None else "?"
            print(f"  {r['file']}: candidates=[{offsets}]  chose_trim={chosen}")

    if crashed:
        print(f"\nErrors:")
        for r in crashed:
            print(f"  {r['file']} [{r['status']}]: {r['error']}")

    print()


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 validate_pipeline.py <folder_with_audio_files>")
        print(f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        print("Suggested location: Audio/validation_tracks/ (already git-ignored)")
        return 1

    folder = Path(sys.argv[1])
    if not folder.is_dir():
        print(f"Error: not a directory: {folder}")
        return 1

    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS)
    if not files:
        print(f"No audio files found in {folder} (looked for {', '.join(sorted(SUPPORTED_EXTENSIONS))})")
        return 1

    # Root logger must allow INFO through, or mashup_engine.py's INFO-level
    # logs (which _CaptureHandler below relies on to recover the winning
    # kick candidate) get filtered before any handler ever sees them. The
    # console handler's own level is bumped back up to WARNING right after,
    # so those same INFO logs don't spam the live terminal output -- they're
    # captured separately, per-track, via _CaptureHandler instead.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    for h in logging.getLogger().handlers:
        h.setLevel(logging.WARNING)

    engine = MashupEngine()
    results = []

    print(f"Found {len(files)} track(s) in {folder}\n")
    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.name} ...", end=" ", flush=True)
        r = analyze_one(engine, path)
        results.append(r)
        print(f"{r['status']} ({r['elapsed_sec']:.1f}s)")

    print_summary(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
