#!/usr/bin/env python3
"""
Quick test of multi-engine stem separation pipeline.
Usage: python test_multi_engine.py <audio_file>
"""
import sys
import logging
from pathlib import Path
from mashup_engine import MashupEngine

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s'
)

def main():
    if len(sys.argv) < 2:
        print("Usage: python test_multi_engine.py <audio_file>")
        print("\nExample: python test_multi_engine.py sample.mp3")
        print("\nThis tests both legacy (Demucs-only) and multi-engine separation modes.")
        return 1

    audio_file = sys.argv[1]
    if not Path(audio_file).is_file():
        print(f"Error: File not found: {audio_file}")
        return 1

    engine = MashupEngine()

    # Test 1: Legacy mode (7 stems)
    print("\n" + "="*60)
    print("TEST 1: Legacy Demucs-only mode (7 stems)")
    print("="*60)
    try:
        stems_legacy = engine.separate_stems([audio_file], use_multi_engine=False)
        print(f"\n✅ Legacy mode successful!")
        print(f"Stems: {list(stems_legacy[0].keys())}")
        for stem_name, path in stems_legacy[0].items():
            exists = "✓" if Path(path).is_file() else "✗"
            print(f"  {exists} {stem_name}: {Path(path).name}")
    except Exception as e:
        print(f"❌ Legacy mode failed: {e}")
        return 1

    # Test 2: Multi-engine mode (9 stems)
    print("\n" + "="*60)
    print("TEST 2: Multi-engine mode (9 stems)")
    print("="*60)
    print("This requires: pip install audio-separator>=0.17.0")
    try:
        stems_multi = engine.separate_stems([audio_file], use_multi_engine=True)
        print(f"\n✅ Multi-engine mode successful!")
        print(f"Stems: {list(stems_multi[0].keys())}")
        for stem_name, path in stems_multi[0].items():
            if path:
                exists = "✓" if Path(path).is_file() else "✗"
                print(f"  {exists} {stem_name}: {Path(path).name}")
            else:
                print(f"  ✗ {stem_name}: (not generated)")
    except ImportError as e:
        print(f"⚠️  Multi-engine mode skipped (dependencies not installed):")
        print(f"  {e}")
        print(f"\nTo enable: pip install audio-separator>=0.17.0 julius>=0.2.8")
    except Exception as e:
        print(f"❌ Multi-engine mode failed: {e}")
        return 1

    print("\n" + "="*60)
    print("All tests complete!")
    print("="*60)
    return 0

if __name__ == '__main__':
    sys.exit(main())
