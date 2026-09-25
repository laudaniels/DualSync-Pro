import React, { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import Waveform from './Waveform';
import SongMixer from './SongMixer';
import { stemNames, stemLabels, orderStems } from './stemConstants';
import { getKeyRecommendations, camelotCode, camelotDistanceBetween } from './camelotWheel';
import { DualStemPlayer } from '../audio/DualStemPlayer';
import '../styles/DualMixer.css';

export default function DualMixer() {
  const [stems, setStems] = useState([null, null]);
  const [metadata, setMetadata] = useState([null, null]);
  const [loading, setLoading] = useState([false, false]);
  const [error, setError] = useState('');
  const [playing, setPlaying] = useState(false);
  const [audioReady, setAudioReady] = useState([false, false]); // All 7 stems fetched+decoded for that slot
  const [pendingSong, setPendingSong] = useState([null, null]); // { wavFilename, filename } after upload+convert, awaiting the as_is/align choice
  const [processingStage, setProcessingStage] = useState([null, null]); // 'converting' | 'separating' | null -- purely cosmetic, for the loading label
  const playerRef = useRef(null);
  if (playerRef.current === null) playerRef.current = new DualStemPlayer();
  const [editingBpm, setEditingBpm] = useState([false, false]);
  const [editingKey, setEditingKey] = useState([false, false]);
  const [overrideBpm, setOverrideBpm] = useState([null, null]);
  const [overrideKey, setOverrideKey] = useState([null, null]);
  const [targetKey, setTargetKey] = useState(null);
  const [transposedStems, setTransposedStems] = useState([null, null]);
  const [transposingStatus, setTransposingStatus] = useState([null, null]);
  const [targetBpm, setTargetBpm] = useState(null);
  const [beatmatchedStems, setBeatmatchedStems] = useState([null, null]);
  const [beatmatchStatus, setBeatmatchStatus] = useState([null, null]);
  const [processingProgress, setProcessingProgress] = useState(0); // 0-100
  const [isProcessing, setIsProcessing] = useState(false);
  const [transposingProgress, setTransposingProgress] = useState(0); // 0-100
  const [isTransposing, setIsTransposing] = useState(false);
  const [lastProcessedBpm, setLastProcessedBpm] = useState(null); // Track last processed BPM
  const [lastProcessedKey, setLastProcessedKey] = useState(null); // Track last processed Key
  const isLocked = isProcessing || isTransposing || playing; // Disable controls during processing or playback
  const bpmChanged = targetBpm !== lastProcessedBpm; // BPM changed since last process
  const keyChanged = targetKey !== lastProcessedKey; // Key changed since last process
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [audioStats, setAudioStats] = useState({ file_count: 0, total_size_formatted: '0 MB' });
  const [processingLogs, setProcessingLogs] = useState([]);
  const logsEndRef = useRef(null);
  const currentProcessingSlotRef = useRef(0); // Which slot handleProcessAllChanges is currently on

  const beatSnapTimeoutRef = useRef(null); // Debounce for magnetic snap visual feedback
  // Slider's own unit is BARS (0-32) -- far more precise to drag/snap at
  // this range than raw beats were (0.15 BEATS used to be the magnetic-snap
  // threshold when the max was 8 beats; at 128 beats that threshold became
  // smaller than a single pixel of slider drag, so snapping silently never
  // fired). beatOffset (derived below, x4) is what the player/render still
  // consume, since those work in beats.
  const [barOffset, setBarOffset] = useState(0); // 0-32 bars for Song 2
  const [barOffsetDisplay, setBarOffsetDisplay] = useState(0); // Fine-tuned display value
  const beatOffset = barOffset * 4; // what the player/render actually consume
  const [driftCorrection, setDriftCorrection] = useState(50); // 0-100, live playbackRate drift correction strength for Song 2
  const [driftInfo, setDriftInfo] = useState({ instantaneousMs: 0, cumulativeBeats: 0 }); // live readout, polled from the player while playing
  const [isSnappedToBeat, setIsSnappedToBeat] = useState(false); // Visual feedback for snap
  const [kickWaveforms, setKickWaveforms] = useState(null); // Kick drum waveforms for display
  const [selectedStemsForWaveform, setSelectedStemsForWaveform] = useState(['kick']); // Which stems to display in waveform
  const [waveformZoom, setWaveformZoom] = useState(10); // Waveform zoom level in seconds

  // Volume states for each song (with split drums)
  const [volumes, setVolumes] = useState({
    0: { vocals: 1.0, kick: 1.0, snare: 1.0, hihat: 1.0, tom: 1.0, bass: 1.0, other: 1.0 },
    1: { vocals: 1.0, kick: 1.0, snare: 1.0, hihat: 1.0, tom: 1.0, bass: 1.0, other: 1.0 }
  });

  // Whichever stems the loaded song(s) actually have -- 7 in legacy mode, 9
  // in multi-engine mode (adds guitar/piano). Falls back to the legacy list
  // before any song has loaded stems, so nothing downstream crashes on an
  // empty array while pendingSong/upload UI is still showing.
  const activeStemNames = useMemo(() => {
    const keys = new Set();
    for (const s of stems) {
      if (s) Object.keys(s).forEach(k => keys.add(k));
    }
    return keys.size > 0 ? orderStems([...keys]) : stemNames;
  }, [stems]);

  // Crossfader: 0 = song1 only, 50 = both, 100 = song2 only
  const [crossfader, setCrossfader] = useState(50);

  // Fetch audio stats
  const fetchStats = useCallback(async () => {
    try {
      const response = await fetch('/api/audio-stats');
      const data = await response.json();
      setAudioStats(data);
    } catch (err) {
      console.error('Stats fetch error:', err);
    }
  }, []);

  // Fetch stats on component mount
  React.useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  // Magnetic snap behavior for beat offset -- snaps to the nearest whole BAR
  // now (not beat), since that's the slider's own unit at this range.
  React.useEffect(() => {
    const nearestBar = Math.round(barOffsetDisplay);
    const snapThreshold = 0.1;
    const distanceToNearestBar = Math.abs(barOffsetDisplay - nearestBar);

    if (distanceToNearestBar < snapThreshold && nearestBar >= 0 && nearestBar <= 32) {
      // Snap to bar
      setBarOffset(nearestBar);
      setIsSnappedToBeat(true);

      // Clear previous timeout
      if (beatSnapTimeoutRef.current) {
        clearTimeout(beatSnapTimeoutRef.current);
      }

      // Hold snap feedback for 200ms
      beatSnapTimeoutRef.current = setTimeout(() => {
        setIsSnappedToBeat(false);
      }, 200);
    } else {
      // Not near a bar, just update barOffset directly
      setBarOffset(parseFloat(barOffsetDisplay.toFixed(2)));
      setIsSnappedToBeat(false);
    }

    return () => {
      if (beatSnapTimeoutRef.current) {
        clearTimeout(beatSnapTimeoutRef.current);
      }
    };
  }, [barOffsetDisplay]);

  // Poll for processing logs while loading
  React.useEffect(() => {
    if (!loading[0] && !loading[1]) return;

    const interval = setInterval(async () => {
      try {
        const response = await fetch('/api/process-status');
        const data = await response.json();
        if (data.logs && data.logs.length > 0) {
          setProcessingLogs(data.logs);
        }
      } catch (err) {
        console.error('Log fetch error:', err);
      }
    }, 300);

    return () => clearInterval(interval);
  }, [loading]);

  // Auto-scroll logs to latest message -- but keep the view on the song
  // panels themselves (uploading/waiting/choosing) rather than being pulled
  // down to the growing log, until Song 2 is actually being processed
  // (loading[1]). Before that, Song 2 may just be waiting for Song 1 to
  // finish so all 3 processing choices can be shown together.
  React.useEffect(() => {
    if (logsEndRef.current && loading[1]) {
      // block: 'end' aligns the BOTTOM of the log panel to the viewport's
      // bottom edge -- the default ('start') aligns logsEndRef's top to the
      // viewport's top instead, which (since it's a zero-height marker right
      // after the last log line) pushes the whole log list above it clean
      // off-screen.
      logsEndRef.current.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  }, [processingLogs, loading]);

  // Helper: Generate filename with BPM labels
  const generateBpmLabel = (meta, isProcessed = false) => {
    if (!meta) return '?';

    if (!isProcessed) {
      // Original stems: show detected BPM
      return `${meta.bpm || meta.detected_bpm || '?'}`;
    }

    // Processed stems: show target and measured
    if (meta.measured_bpm && meta.target_bpm) {
      const isBpmManual = meta.source_bpm && parseFloat(meta.source_bpm) !== meta.bpm;
      if (isBpmManual) {
        // User manually overrode BPM: show both manual and measured
        return `${meta.target_bpm}-manual-${meta.measured_bpm.toFixed(1)}-measured`;
      } else {
        // Auto-detected: show measured
        return `${meta.measured_bpm.toFixed(1)}`;
      }
    }
    return meta.bpm || '?';
  };

  // Step 1: a file was dropped/selected for a slot. Upload it and convert to
  // WAV; the actual analyze/align/separate work waits for the user's
  // as-is/align choice (see handleChooseMode).
  const handleFileDropped = useCallback(async (file, slot) => {
    setLoading(prev => { const updated = [...prev]; updated[slot] = true; return updated; });
    setError('');
    setProcessingStage(prev => { const updated = [...prev]; updated[slot] = 'converting'; return updated; });

    // Show filename immediately while converting
    setMetadata(prev => { const updated = [...prev]; updated[slot] = { filename: file.name }; return updated; });

    console.log(`📥 Song ${slot + 1} upload started`);

    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('slot', slot);

      console.log(`🔄 Uploading + converting Song ${slot + 1} to WAV...`);
      const response = await fetch('/api/upload-audio', {
        method: 'POST',
        body: formData
      });

      if (!response.ok) throw new Error(`Server error: ${response.status}`);

      const data = await response.json();
      console.log(`✅ Song ${slot + 1} ready for processing choice:`, data);

      setPendingSong(prev => {
        const updated = [...prev];
        updated[slot] = { wavFilename: data.wav_filename, filename: data.filename };
        return updated;
      });
    } catch (err) {
      console.error(`❌ Error:`, err);
      setError(`Error uploading Song ${slot + 1}: ${err.message}`);
    } finally {
      setLoading(prevLoading => {
        const updated = [...prevLoading];
        updated[slot] = false;
        return updated;
      });
      setProcessingStage(prev => { const updated = [...prev]; updated[slot] = null; return updated; });
    }
  }, []);

  // Step 2: the user picked 'as_is' or 'align' for the converted WAV -- run
  // the (optional) beatgrid alignment, BPM/key analysis, and stem separation.
  const handleChooseMode = useCallback(async (slot, mode) => {
    const pending = pendingSong[slot];
    if (!pending) return;

    setLoading(prev => { const updated = [...prev]; updated[slot] = true; return updated; });
    setPendingSong(prev => { const updated = [...prev]; updated[slot] = null; return updated; });
    setError('');
    setProcessingStage(prev => { const updated = [...prev]; updated[slot] = mode; return updated; });

    try {
      console.log(`🔊 Processing Song ${slot + 1} (mode: ${mode})...`);
      const response = await fetch('/api/process-song', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          wav_filename: pending.wavFilename,
          filename: pending.filename,
          slot,
          mode,
          // Only meaningful for mode === 'snap' -- Song 1's already-known
          // grid to warp this song onto.
          reference_bpm: mode === 'snap' ? metadata[0]?.bpm : undefined,
          reference_anchor: mode === 'snap' ? metadata[0]?.beat_anchor : undefined
        })
      });

      if (!response.ok) throw new Error(`Server error: ${response.status}`);

      const data = await response.json();
      console.log(`✅ Song ${slot + 1} stems:`, data);

      setStems(prevStems => {
        const newStems = [...prevStems];
        newStems[slot] = data.stems;
        console.log(`✅ Song ${slot + 1} stems loaded. State:`, { stems: newStems, song1: !!newStems[0], song2: !!newStems[1] });
        return newStems;
      });

      setMetadata(prevMetadata => {
        const newMetadata = [...prevMetadata];
        newMetadata[slot] = {
          ...data,
          timestamp: data.timestamp || Date.now().toString(),
          mode,
          sourceWavFilename: data.source_wav_filename,
          unalignedWavFilename: mode === 'align' ? pending.wavFilename : null,
          // How much the initial align/snap step corrected (null if mode
          // was 'as_is', or if align/snap failed) -- { mean_ms, max_ms }.
          gridCorrection: data.grid_correction,
          // Immutable snapshot of what was originally detected -- always the
          // basis for the NEXT reprocess (see getEffectiveBpm/Key), and what
          // the upload box keeps showing regardless of later reprocessing.
          detectedBpm: data.bpm,
          detectedKey: data.key,
          // 'major' | 'minor' | null -- only Essentia detects mode, Librosa
          // can't. Needed for Camelot-wheel-aware key recommendations.
          detectedScale: data.scale,
          // What's actually loaded/playing right now -- starts the same as
          // detected, updated after each successful reprocess (see
          // processStems below) via getCurrentBpm/Key.
          currentBpm: data.bpm,
          currentKey: data.key
        };
        return newMetadata;
      });

      // Fetch + decode every stem into an AudioBuffer up front, so Play can
      // just schedule already-in-memory buffers instead of relying on 14
      // concurrent streaming connections (browsers cap that at 6 per origin).
      console.log(`🎵 Song ${slot + 1} stems received:`, Object.keys(data.stems));
      setAudioReady(prev => { const updated = [...prev]; updated[slot] = false; return updated; });
      await Promise.all(Object.entries(data.stems).map(async ([stemName, url]) => {
        try {
          await playerRef.current.loadStem(slot, stemName, url);
          console.log(`✅ Decoded ${stemName} for Song ${slot + 1}: ${url}`);
        } catch (err) {
          console.error(`❌ Could not load ${stemName} for Song ${slot + 1}:`, err);
        }
      }));
      setAudioReady(prev => {
        const updated = [...prev];
        updated[slot] = playerRef.current.isSlotReady(slot, Object.keys(data.stems));
        return updated;
      });

      // Refresh stats
      fetchStats();
    } catch (err) {
      console.error(`❌ Error:`, err);
      setError(`Error processing Song ${slot + 1}: ${err.message}`);
      // Restore the pending choice so the user can retry without re-uploading
      setPendingSong(prev => { const updated = [...prev]; updated[slot] = pending; return updated; });
    } finally {
      setLoading(prevLoading => {
        const updated = [...prevLoading];
        updated[slot] = false;
        return updated;
      });
      setProcessingStage(prev => { const updated = [...prev]; updated[slot] = null; return updated; });
    }
  }, [pendingSong, fetchStats, metadata]);


  // Play/pause both songs. All 14 stems are already fully decoded into
  // AudioBuffers (see handleStemsLoaded), so this just schedules playback --
  // no per-stem network readiness to wait on anymore.
  const togglePlayback = () => {
    if (playing) {
      playerRef.current.pause();
      setPlaying(false);
    } else {
      // ensureContext()/play() must run synchronously in this click handler
      // (no await before them) -- creating/resuming an AudioContext requires
      // a live user-gesture call stack.
      playerRef.current.ensureContext();
      playerRef.current.play(activeStemNames);
      setPlaying(true);
    }
  };

  // Update volume
  const handleVolumeChange = useCallback((slot, stem, value) => {
    setVolumes(prev => ({
      ...prev,
      [slot]: { ...prev[slot], [stem]: value }
    }));
    playerRef.current.setVolume(slot, stem, value);
  }, []);

  // Update crossfader
  const handleCrossfaderChange = (value) => {
    setCrossfader(value);
    playerRef.current.setCrossfader(value);
  };

  const handleSeek = (e) => {
    if (duration === 0) return;

    // Get click position relative to progress bar
    const rect = e.currentTarget.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const percentage = clickX / rect.width;
    const newTime = percentage * duration;

    playerRef.current.seek(newTime, activeStemNames);
    setCurrentTime(newTime);
  };

  // Track progress. Playback loops natively now (AudioBufferSourceNode.loop
  // with loopEnd set to the reference duration), so there's no boundary to
  // detect and no manual restart to do -- getPosition() already wraps via
  // modulo, this just mirrors it into state for the progress bar/Waveform.
  useEffect(() => {
    if (!playing) return;

    const interval = setInterval(() => {
      setCurrentTime(playerRef.current.getPosition());
      setDuration(playerRef.current.getReferenceDuration());
      setDriftInfo(playerRef.current.getDriftInfo());
    }, 100);

    return () => clearInterval(interval);
  }, [playing]);

  // Update beat offset delay in real-time
  useEffect(() => {
    // Use the ACTUAL current BPM of the loaded audio, not the fixed
    // originally-detected value -- after a beatmatch reprocess, Song 2 is
    // really playing at its new tempo, and the beat-offset delay (a number
    // of BEATS converted to seconds) has to be timed against that real
    // tempo or the two songs' beat grids drift out of phase. (Inlined
    // rather than calling getCurrentBpm so this effect's deps stay exact --
    // that helper isn't memoized, so listing it would fire this every render.)
    const song2Bpm = overrideBpm[1] ?? metadata[1]?.currentBpm ?? metadata[1]?.detectedBpm ?? metadata[1]?.bpm ?? 120;
    playerRef.current.setBeatOffset(beatOffset, song2Bpm);
  }, [beatOffset, metadata, overrideBpm]);

  // Feed each song's current bpm + beat anchor to the player's continuous
  // drift corrector. A time-stretch scales EVERY time position in the file,
  // including where the first beat lands, so the anchor has to be scaled
  // by the same ratio the tempo changed by, or the corrector would measure
  // phase against a stale reference point.
  useEffect(() => {
    for (const slot of [0, 1]) {
      const detectedBpm = metadata[slot]?.detectedBpm;
      const detectedAnchor = metadata[slot]?.beat_anchor;
      if (!detectedBpm || detectedAnchor == null) {
        playerRef.current.setBeatGridInfo(slot, null, null);
        continue;
      }
      const currentBpm = overrideBpm[slot] ?? metadata[slot]?.currentBpm ?? detectedBpm;
      const scaledAnchor = detectedAnchor * (detectedBpm / currentBpm);
      playerRef.current.setBeatGridInfo(slot, currentBpm, scaledAnchor);
    }
  }, [metadata, overrideBpm]);

  // Drift correction strength slider (0-100 -> 0-1)
  useEffect(() => {
    playerRef.current.setDriftCorrectionStrength(driftCorrection / 100);
  }, [driftCorrection]);

  // Load waveforms for selected stems -- reuses the AudioBuffers already
  // decoded for playback (see handleStemsLoaded) instead of re-fetching and
  // re-decoding the same files a second time just for the display.
  useEffect(() => {
    if (!audioReady[0] && !audioReady[1]) return;
    if (!selectedStemsForWaveform.length) return;

    const waveformData = { stems: {}, duration: 0 };

    for (const stem of selectedStemsForWaveform) {
      waveformData.stems[stem] = { data1: null, data2: null };

      for (let slot = 0; slot < 2; slot++) {
        const audioBuffer = playerRef.current.buffers[slot]?.[stem];
        if (!audioBuffer) continue;

        const rawData = audioBuffer.getChannelData(0);
        const samples = Math.min(rawData.length, 2048); // Limit samples for display
        const stemData = new Float32Array(samples);
        for (let i = 0; i < samples; i++) {
          stemData[i] = rawData[Math.floor((i / samples) * rawData.length)];
        }

        waveformData.stems[stem][`data${slot + 1}`] = stemData;
        waveformData.duration = Math.max(waveformData.duration, audioBuffer.duration);
      }
    }

    if (Object.keys(waveformData.stems).length > 0) {
      setKickWaveforms(waveformData);
    }
  }, [audioReady, metadata, selectedStemsForWaveform]);

  const formatTime = (seconds) => {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

  // The mode (major/minor) a song was detected in never changes -- pitch
  // shifting can move a track to a different key, but not turn a minor
  // recording major, so unlike bpm/key there's no separate "current" vs
  // "detected" scale to track.
  const getEffectiveScale = (slot) => metadata[slot]?.detectedScale ?? null;

  // Up to 5 candidate shared target keys, ranked by Camelot-wheel
  // compatibility (circle-of-fifths distance) rather than raw semitone
  // distance -- see camelotWheel.js for why that's a better metric for how
  // natural a transposition will sound.
  const getKeyRecommendationsList = () => {
    const key0 = getEffectiveKey(0);
    const key1 = getEffectiveKey(1);
    if (!key0 || !key1 || !stems[0] || !stems[1]) return [];
    return getKeyRecommendations(key0, getEffectiveScale(0), key1, getEffectiveScale(1), 5);
  };

  // Are the songs ALREADY Camelot-compatible as they stand (same key, or a
  // relative-major/minor / adjacent-fifth pair)? The ranked list above can
  // never surface this, since it only considers moving BOTH songs to one
  // identical shared pitch class -- it can't represent "leave them as they
  // are, they're already a compatible pair in different keys."
  const getOwnCompatibility = () => {
    const key0 = getEffectiveKey(0);
    const key1 = getEffectiveKey(1);
    if (!key0 || !key1 || !stems[0] || !stems[1]) return null;
    return camelotDistanceBetween(key0, getEffectiveScale(0), key1, getEffectiveScale(1));
  };

  // The basis for the NEXT processing action -- always the originally
  // detected value (or the user's manual correction to it), never a
  // previous processing round's result. Reprocessing always starts fresh
  // from the initial WAV, so a second "Process" click has to compute its
  // semitone/tempo shift from the ORIGINAL analysis, not from wherever the
  // last click left off -- otherwise (key especially, which has no
  // convergence/self-correction like BPM does) it silently lands on the
  // wrong result. See getCurrentBpm/getCurrentKey for "what's actually
  // loaded and playing right now" instead.
  const getEffectiveBpm = (slot) => {
    if (overrideBpm[slot] !== null) return overrideBpm[slot];
    return metadata[slot]?.detectedBpm ?? metadata[slot]?.bpm;
  };
  const getEffectiveKey = (slot) => {
    if (overrideKey[slot] !== null) return overrideKey[slot];
    return metadata[slot]?.detectedKey ?? metadata[slot]?.key;
  };

  // What the currently loaded/playing stems actually are right now (updates
  // after each successful reprocess) -- used for real-time playback timing
  // (beat offset) and the "current stems" status line, as opposed to
  // getEffectiveBpm/Key's fixed planning basis above.
  const getCurrentBpm = (slot) => {
    if (overrideBpm[slot] !== null) return overrideBpm[slot];
    return metadata[slot]?.currentBpm ?? metadata[slot]?.detectedBpm ?? metadata[slot]?.bpm;
  };
  const getCurrentKey = (slot) => {
    if (overrideKey[slot] !== null) return overrideKey[slot];
    return metadata[slot]?.currentKey ?? metadata[slot]?.detectedKey ?? metadata[slot]?.key;
  };

  const getSemitoneShift = (fromKey, toKey) => {
    const keyToIndex = { C: 0, 'C#': 1, D: 2, 'D#': 3, E: 4, F: 5, 'F#': 6, G: 7, 'G#': 8, A: 9, 'A#': 10, B: 11 };
    const from = keyToIndex[fromKey];
    const to = keyToIndex[toKey];
    if (from === undefined || to === undefined) return 0;
    let diff = to - from;
    if (diff > 6) diff -= 12;
    if (diff < -6) diff += 12;
    return diff;
  };

  const handleBpmOverride = useCallback((slot, value) => {
    setOverrideBpm(prev => {
      const updated = [...prev];
      updated[slot] = value ? parseFloat(value) : null;
      return updated;
    });
  }, []);

  const handleKeyOverride = useCallback((slot, value) => {
    setOverrideKey(prev => {
      const updated = [...prev];
      updated[slot] = value || null;
      return updated;
    });
  }, []);

  const toggleEditingBpm = useCallback((slot, editing) => {
    setEditingBpm(prev => { const updated = [...prev]; updated[slot] = editing; return updated; });
  }, []);

  const toggleEditingKey = useCallback((slot, editing) => {
    setEditingKey(prev => { const updated = [...prev]; updated[slot] = editing; return updated; });
  }, []);

  // Combined processing for beatmatch + transpose
  const processStems = async (slot, newTargetBpm, newTargetKey) => {
    // Pause playback during processing
    if (playing) {
      togglePlayback();
    }

    if (!stems[slot] || !metadata[slot]) return;

    const sourceBpm = getEffectiveBpm(slot);
    const sourceKey = getEffectiveKey(slot);

    // Determine if processing needed
    const needsBeatmatch = newTargetBpm && sourceBpm && sourceBpm !== newTargetBpm;
    const needsTranspose = newTargetKey && sourceKey && sourceKey !== newTargetKey;

    if (!needsBeatmatch && !needsTranspose) return;

    // Set initial status
    setTransposingStatus(prev => {
      const n = [...prev];
      n[slot] = 'processing';
      return n;
    });
    setBeatmatchStatus(prev => {
      const n = [...prev];
      n[slot] = 'processing';
      return n;
    });

    // Start polling for status updates (per-slot, so it can't be clobbered
    // by the other song processing at the same time)
    const statusPoller = setInterval(async () => {
      try {
        const statusRes = await fetch('/api/process-status');
        const status = await statusRes.json();
        const slotState = status.slots?.[slot];
        if (slotState) {
          console.log(`⏳ Song ${slot + 1} processing: ${slotState.current_step} (${slotState.progress}%)`);
        }
      } catch (err) {
        // Silently ignore polling errors
      }
    }, 500);

    try {
      const response = await fetch('/api/process-stems', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_bpm: needsBeatmatch ? sourceBpm : null,
          target_bpm: needsBeatmatch ? newTargetBpm : null,
          source_key: needsTranspose ? sourceKey : null,
          target_key: needsTranspose ? newTargetKey : null,
          timestamp: metadata[slot].timestamp,
          filename: metadata[slot].filename,
          source_wav_filename: metadata[slot].sourceWavFilename,
          slot: slot
        })
      });

      clearInterval(statusPoller);
      const data = await response.json();

      if (data.processed_stems) {
        // Store measured BPM/key in metadata for accurate file naming, and
        // update the "currently playing" values (NOT the immutable
        // detected/effective ones -- those stay fixed as the basis for the
        // NEXT reprocess, which always starts fresh from the initial WAV).
        setMetadata(prevMetadata => {
          const newMetadata = [...prevMetadata];
          if (newMetadata[slot]) {
            newMetadata[slot] = {
              ...newMetadata[slot],
              ...(data.measured_bpm && {
                measured_bpm: data.measured_bpm,
                source_bpm: data.source_bpm,
                target_bpm: data.target_bpm,
                currentBpm: data.measured_bpm
              }),
              ...(data.measured_key && { currentKey: data.measured_key })
            };
          }
          return newMetadata;
        });

        // Re-fetch + re-decode the processed stems, replacing the buffers
        // used for playback (the backend already confirms the files are
        // fully written before responding, so no artificial delay needed).
        await Promise.all(Object.entries(data.processed_stems).map(([stemName, url]) =>
          playerRef.current.loadStem(slot, stemName, url).catch(err =>
            console.error(`Could not reload processed ${stemName} for Song ${slot + 1}:`, err))
        ));

        setTransposedStems(prev => {
          const n = [...prev];
          n[slot] = data.processed_stems;
          return n;
        });
        setBeatmatchedStems(prev => {
          const n = [...prev];
          n[slot] = data.processed_stems;
          return n;
        });

        setTransposingStatus(prev => {
          const n = [...prev];
          n[slot] = 'done';
          return n;
        });
        setBeatmatchStatus(prev => {
          const n = [...prev];
          n[slot] = 'done';
          return n;
        });

        console.log(`✅ Song ${slot + 1} processed successfully (measured BPM: ${data.measured_bpm?.toFixed(1) || '?'})`);
      } else {
        throw new Error(data.error || 'Processing failed');
      }
    } catch (err) {
      clearInterval(statusPoller);
      console.error(`Processing error (Song ${slot + 1}):`, err);
      setTransposingStatus(prev => {
        const n = [...prev];
        n[slot] = 'error';
        return n;
      });
      setBeatmatchStatus(prev => {
        const n = [...prev];
        n[slot] = 'error';
        return n;
      });
    }
  };

  // Handle target BPM change
  const handleTargetBpmChange = (newTargetBpm) => {
    setTargetBpm(newTargetBpm ? parseFloat(newTargetBpm) : null);
  };

  // Handle target key change
  const handleTargetKeyChange = (newTargetKey) => {
    setTargetKey(newTargetKey || null);
  };

  // Unified process for both BPM and Key changes
  const handleProcessAllChanges = async () => {
    const needsBpmChange = targetBpm && targetBpm > 0 && bpmChanged;
    const needsKeyChange = targetKey && keyChanged;

    if (!needsBpmChange && !needsKeyChange) {
      alert('⚠️  Please set a target BPM or key, and ensure it differs from current');
      return;
    }

    setIsProcessing(true);
    setProcessingProgress(0);

    // Songs are processed one at a time below, and the backend tracks each
    // one's 0-100% independently -- so map that onto one continuous bar
    // spanning all songs being processed (0-50/50-100 for two, 0-100 for
    // one) instead of restarting at 0% for every song.
    const slotsToProcess = [0, 1].filter(slot => stems[slot]);
    const totalSlots = slotsToProcess.length || 1;
    let completedSlots = 0;
    currentProcessingSlotRef.current = slotsToProcess[0] ?? 0;

    const progressInterval = setInterval(async () => {
      try {
        const res = await fetch('/api/process-status');
        const data = await res.json();
        const slotState = data.slots?.[currentProcessingSlotRef.current];
        if (slotState?.progress != null) {
          const combined = ((completedSlots * 100) + slotState.progress) / totalSlots;
          setProcessingProgress(combined);
        }
      } catch (e) {
        // Silent - API might not have data yet
      }
    }, 500);

    try {
      // Process all loaded songs with both BPM and Key
      for (const slot of slotsToProcess) {
        currentProcessingSlotRef.current = slot;
        await processStems(slot, targetBpm, targetKey);
        completedSlots++;
      }
      // Complete progress
      setProcessingProgress(100);
      // Track last processed values
      if (needsBpmChange) setLastProcessedBpm(targetBpm);
      if (needsKeyChange) setLastProcessedKey(targetKey);
      // (metadata.currentKey/currentBpm are updated per-slot, from the
      // backend's actual measured result, inside processStems above --
      // no need to blanket-assign the requested target here.)
    } finally {
      clearInterval(progressInterval);
      setTimeout(() => {
        setIsProcessing(false);
        setProcessingProgress(0);
      }, 500);
    }
  };

  // Shared download handler for the stems/mix export buttons -- tracks which
  // one is in flight (so all three can be disabled while it runs) and surfaces
  // backend errors instead of silently doing nothing.
  const [downloadingKey, setDownloadingKey] = useState(null); // 'original' | 'processed' | 'final' | null

  const handleDownload = async (key, endpoint, body) => {
    setDownloadingKey(key);
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const data = await res.json();
      if (res.ok && data.file) {
        window.location.href = data.file;
      } else {
        throw new Error(data.error || `Server error: ${res.status}`);
      }
    } catch (err) {
      console.error(`Download error (${key}):`, err);
      alert(`❌ Download failed: ${err.message}`);
    } finally {
      setDownloadingKey(null);
    }
  };

  // Clean and reset: delete all generated audio files AND take the whole
  // GUI back to its just-loaded state, so starting over with two new songs
  // never has to contend with leftover state from the previous pair
  // (target bpm/key, overrides, volumes, beat offset, drift-correction
  // stats, waveform selection, logs -- all of it).
  const handleCleanup = async () => {
    if (!confirm('🗑️ Clean and reset: delete all generated audio files and start over? This cannot be undone.')) return;

    try {
      const response = await fetch('/api/cleanup', { method: 'POST' });
      const data = await response.json();

      if (response.ok) {
        alert('✅ Cleaned up and reset! All audio files deleted, ready to start over.');
        playerRef.current.reset();

        // Core song/stem state
        setStems([null, null]);
        setMetadata([null, null]);
        setLoading([false, false]);
        setError('');
        setAudioReady([false, false]);
        setPendingSong([null, null]);
        setProcessingStage([null, null]);

        // BPM/Key override editing
        setEditingBpm([false, false]);
        setEditingKey([false, false]);
        setOverrideBpm([null, null]);
        setOverrideKey([null, null]);

        // Target BPM/Key reprocessing
        setTargetKey(null);
        setTargetBpm(null);
        setTransposedStems([null, null]);
        setTransposingStatus([null, null]);
        setBeatmatchedStems([null, null]);
        setBeatmatchStatus([null, null]);
        setProcessingProgress(0);
        setIsProcessing(false);
        setTransposingProgress(0);
        setIsTransposing(false);
        setLastProcessedBpm(null);
        setLastProcessedKey(null);

        // Playback
        setPlaying(false);
        setCurrentTime(0);
        setDuration(0);
        setProcessingLogs([]);

        // Beat offset & live drift correction
        setBarOffset(0);
        setBarOffsetDisplay(0);
        setDriftCorrection(50);
        setDriftInfo({ instantaneousMs: 0, cumulativeBeats: 0 });
        setIsSnappedToBeat(false);

        // Waveform
        setKickWaveforms(null);
        setSelectedStemsForWaveform(['kick']);
        setWaveformZoom(10);

        // Mixer
        setVolumes({
          0: { vocals: 1.0, kick: 1.0, snare: 1.0, hihat: 1.0, tom: 1.0, bass: 1.0, other: 1.0 },
          1: { vocals: 1.0, kick: 1.0, snare: 1.0, hihat: 1.0, tom: 1.0, bass: 1.0, other: 1.0 }
        });
        setCrossfader(50);
        setDownloadingKey(null);

        setAudioStats({ file_count: 0, total_size_formatted: '0 MB' });
      } else {
        alert(`❌ Cleanup failed: ${data.error}`);
      }
    } catch (err) {
      console.error('Cleanup error:', err);
      alert(`❌ Error: ${err.message}`);
    }
  };

  // Whether the "original" stems download is actually aligned stems (or a
  // mix of both, across the two songs) -- purely for the button label.
  const slotsWithStems = [0, 1].filter(slot => stems[slot]);
  const anySlotAligned = slotsWithStems.some(slot => metadata[slot]?.mode === 'align');
  const allSlotsAligned = slotsWithStems.length > 0 && slotsWithStems.every(slot => metadata[slot]?.mode === 'align');
  const originalStemsLabel = allSlotsAligned ? 'Aligned Stems' : anySlotAligned ? 'Original/Aligned Stems' : 'Original Stems';
  const keyRecommendations = getKeyRecommendationsList();
  const ownCompatibility = getOwnCompatibility();
  const song1Ready = !!stems[0] && metadata[0]?.bpm != null && metadata[0]?.beat_anchor != null;

  return (
    <div className="dual-mixer">
      {error && (
        <div className="error-banner">
          {error}
        </div>
      )}

      <div className="mixer-container">
        {/* Two song mixers side by side */}
        <div className="songs-row">
          {[0, 1].map(slot => (
            <SongMixer
              key={slot}
              slot={slot}
              metadata={metadata[slot]}
              hasStems={!!stems[slot]}
              loading={loading[slot]}
              editingBpm={editingBpm[slot]}
              editingKey={editingKey[slot]}
              overrideBpm={overrideBpm[slot]}
              overrideKey={overrideKey[slot]}
              effectiveBpm={getEffectiveBpm(slot)}
              effectiveKey={getEffectiveKey(slot)}
              volumes={volumes[slot]}
              stemNames={stems[slot] ? orderStems(Object.keys(stems[slot])) : []}
              audioReady={audioReady[slot]}
              pendingSong={pendingSong[slot]}
              processingStage={processingStage[slot]}
              canSnapToSong1={slot === 1 && song1Ready}
              waitingForSong1={slot === 1 && !song1Ready}
              onBpmOverride={handleBpmOverride}
              onKeyOverride={handleKeyOverride}
              onToggleEditingBpm={toggleEditingBpm}
              onToggleEditingKey={toggleEditingKey}
              onFileDropped={handleFileDropped}
              onChooseMode={handleChooseMode}
              onVolumeChange={handleVolumeChange}
            />
          ))}
        </div>

        {/* Waveform Stem Selector - Center Section */}
        {stems[0] && stems[1] && (
          <div style={{
            background: 'rgba(99, 102, 241, 0.08)',
            border: '1px solid rgba(99, 102, 241, 0.2)',
            borderRadius: '8px',
            padding: '15px',
            marginTop: '20px',
            marginBottom: '20px',
            textAlign: 'center'
          }}>
            <div style={{ fontSize: '13px', fontWeight: 'bold', color: '#ccc', marginBottom: '12px' }}>
              📊 Select Stems for Waveform Display {playing ? '(paused to enable)' : '(☑ paused only)'}
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '15px', justifyContent: 'center' }}>
              {activeStemNames.map(stem => (
                <label key={stem} style={{ display: 'flex', alignItems: 'center', gap: '6px', cursor: playing ? 'not-allowed' : 'pointer', opacity: playing ? 0.5 : 1 }}>
                  <input
                    type="checkbox"
                    checked={selectedStemsForWaveform.includes(stem)}
                    onChange={(e) => {
                      if (e.target.checked) {
                        setSelectedStemsForWaveform([...selectedStemsForWaveform, stem]);
                      } else {
                        setSelectedStemsForWaveform(selectedStemsForWaveform.filter(s => s !== stem));
                      }
                    }}
                    disabled={playing}
                    title={playing ? 'Pause to enable' : `Display ${stem} in waveform`}
                    style={{ cursor: playing ? 'not-allowed' : 'pointer', width: '16px', height: '16px' }}
                  />
                  <span style={{ fontSize: '12px' }}>{stemLabels[stem] || stem}</span>
                </label>
              ))}
            </div>
          </div>
        )}

        {/* Unified Processing Log */}
        {(loading[0] || loading[1] || isProcessing) && processingLogs.length > 0 && (
          <div style={{
            background: 'rgba(139, 92, 246, 0.1)',
            border: '1px solid rgba(139, 92, 246, 0.3)',
            borderRadius: '8px',
            padding: '15px',
            marginTop: '20px',
            marginBottom: '20px',
            maxHeight: '250px',
            overflowY: 'auto',
            fontFamily: 'monospace',
            fontSize: '12px',
            color: '#aaa'
          }}>
            <div style={{ color: '#8b5cf6', marginBottom: '10px', fontWeight: 'bold' }}>
              📋 Processing Log
            </div>
            {processingLogs.map((log, i) => (
              <div key={i} style={{ marginBottom: '3px', color: '#ddd' }}>
                {log}
              </div>
            ))}
            <div ref={logsEndRef} />
          </div>
        )}

        {/* Playback & Crossfader Controls */}
        {stems[0] && stems[1] ? (
          <div className="playback-section">
            {/* Now playing: the actual current bpm/key of the loaded stems */}
            <div style={{ fontSize: '12px', color: '#94a3b8', marginBottom: '10px' }}>
              📦 Now playing: Song 1 [{getCurrentKey(0)} {getCurrentBpm(0)} BPM] + Song 2 [{getCurrentKey(1)} {getCurrentBpm(1)} BPM]
            </div>
            <div className="playback-controls">
              <button
                onClick={togglePlayback}
                className="play-btn"
                disabled={isProcessing || isTransposing || !audioReady[0] || !audioReady[1]}
                style={{
                  opacity: (isProcessing || isTransposing || !audioReady[0] || !audioReady[1]) ? 0.5 : 1,
                  cursor: (isProcessing || isTransposing || !audioReady[0] || !audioReady[1]) ? 'not-allowed' : 'pointer'
                }}
              >
                {!audioReady[0] || !audioReady[1] ? '⏳ Preparing audio...' : playing ? '⏸ PAUSE' : '▶ PLAY BOTH'}
              </button>

              <div
                className="progress-bar"
                onClick={(e) => handleSeek(e)}
                style={{ cursor: 'pointer' }}
              >
                <div
                  className="progress-fill"
                  style={{ width: `${duration > 0 ? (currentTime / duration) * 100 : 0}%` }}
                ></div>
              </div>

              <span className="time-display">
                {formatTime(currentTime)} / {formatTime(duration)}
              </span>
            </div>

            {/* Kick Waveform Visualization for Beat Alignment */}
            {kickWaveforms && (
              <Waveform
                kicks={kickWaveforms}
                currentTime={currentTime}
                beatOffset={beatOffset}
                song2Bpm={getCurrentBpm(1) || 120}
                song1Bpm={getCurrentBpm(0) || 120}
                song1BeatAnchor={metadata[0]?.beat_anchor ?? 0}
                zoomLevel={waveformZoom}
                onZoomChange={setWaveformZoom}
              />
            )}

            {/* Crossfader */}
            <div className="crossfader-section">
              <label>🎛️ Crossfader</label>
              <div className="crossfader-labels">
                <span>Song 1</span>
                <span>Both</span>
                <span>Song 2</span>
              </div>
              <input
                type="range"
                min="0"
                max="100"
                step="1"
                value={crossfader}
                onChange={(e) => handleCrossfaderChange(parseFloat(e.target.value))}
                className="crossfader-slider"
              />
              <div className="crossfader-value">
                {crossfader === 50 ? 'Both: 50/50' : crossfader < 50 ? `Song 1: ${100 - crossfader}%` : `Song 2: ${crossfader}%`}
              </div>
            </div>

            {/* Beat Offset for Song 2 with Magnetic Snap -- up to 32 bars.
                The slider's own unit is BARS (precise to drag/snap at this
                range); beatOffset (bars*4) is what's sent to the player and
                the render, since those work in beats. */}
            <div className="crossfader-section" style={{ marginTop: '15px' }}>
              <label>🎵 Beat Offset (Song 2) — Fine-tune + Snap to Bars</label>
              <div className="crossfader-labels">
                <span>Sync</span>
                <span>Offset</span>
                <span>+32 bars</span>
              </div>
              <div style={{ position: 'relative' }}>
                <input
                  type="range"
                  min="0"
                  max="32"
                  step="0.05"
                  value={barOffsetDisplay}
                  onChange={(e) => setBarOffsetDisplay(parseFloat(e.target.value))}
                  disabled={!stems[0] || !stems[1]}
                  className="crossfader-slider"
                  style={{
                    opacity: (!stems[0] || !stems[1]) ? 0.5 : 1,
                    cursor: (!stems[0] || !stems[1]) ? 'not-allowed' : 'pointer'
                  }}
                />
              </div>
              <div className="crossfader-value">
                {isSnappedToBeat ? (
                  `🎯 Snapped: +${barOffset} bar${barOffset !== 1 ? 's' : ''} (${beatOffset} beats) — fine-tune: ${barOffsetDisplay.toFixed(2)} bars`
                ) : barOffsetDisplay === 0 ? (
                  '✓ Sync (no offset)'
                ) : (
                  `⚙️ Fine-tuning: +${barOffsetDisplay.toFixed(2)} bars (${(barOffsetDisplay * 4).toFixed(1)} beats)`
                )}
              </div>
            </div>

            {/* Continuous drift correction -- the beat offset above is a
                one-time static nudge; this keeps correcting Song 2's tiny
                residual tempo error against Song 1 in real time as they play. */}
            <div className="crossfader-section" style={{ marginTop: '15px' }}>
              <label>🧲 Live Beat-Grid Correction (Song 2 sticks to Song 1)</label>
              <div className="crossfader-labels">
                <span>Off</span>
                <span>Gentle</span>
                <span>Aggressive</span>
              </div>
              <div style={{ position: 'relative' }}>
                <input
                  type="range"
                  min="0"
                  max="100"
                  step="1"
                  value={driftCorrection}
                  onChange={(e) => setDriftCorrection(parseFloat(e.target.value))}
                  disabled={!stems[0] || !stems[1]}
                  className="crossfader-slider"
                  style={{
                    opacity: (!stems[0] || !stems[1]) ? 0.5 : 1,
                    cursor: (!stems[0] || !stems[1]) ? 'not-allowed' : 'pointer'
                  }}
                />
              </div>
              <div className="crossfader-value">
                {driftCorrection === 0
                  ? '✓ Off (no live correction)'
                  : `🧲 Correcting up to ±${(0.005 * (driftCorrection / 100) * 100).toFixed(2)}% playback rate as needed`}
              </div>
              {driftCorrection > 0 && playing && (
                <div style={{ fontSize: '11px', color: '#999', marginTop: '4px' }}>
                  Current drift: {driftInfo.instantaneousMs >= 0 ? '+' : ''}{driftInfo.instantaneousMs.toFixed(1)}ms
                  {' · '}Total realigned so far: {driftInfo.cumulativeBeats.toFixed(2)} beats
                </div>
              )}
            </div>

          </div>
        ) : null}

        {/* Processing Section: BPM & Key */}
        {stems[0] || stems[1] ? (
          <div style={{
            background: 'rgba(99, 102, 241, 0.05)',
            border: '1px solid rgba(99, 102, 241, 0.2)',
            padding: '20px',
            borderRadius: '12px',
            marginTop: '20px'
          }}>
            <label style={{ display: 'block', fontSize: '14px', fontWeight: 'bold', color: '#ccc', marginBottom: '5px' }}>
              ⚙️ Processing (BPM & Key)
            </label>
            <p style={{ margin: '0 0 15px 0', fontSize: '11px', color: '#888' }}>
              ℹ️ Each time you process, it starts fresh from the originally detected BPM/Key (not from the last processed result) -- so the target you set here is always an absolute destination, not an additional shift.
            </p>

            {/* BPM Input */}
            <div style={{ marginBottom: '15px' }}>
              <label style={{ fontSize: '12px', color: '#999', marginBottom: '5px', display: 'block' }}>
                ⏱️ Target BPM (for beatmatching)
              </label>
              <input
                type="number"
                value={targetBpm || ''}
                onChange={(e) => handleTargetBpmChange(e.target.value)}
                placeholder="Enter target BPM"
                disabled={isLocked || !stems[0] || !stems[1]}
                style={{
                  width: '100%',
                  padding: '8px',
                  background: 'rgba(99, 102, 241, 0.2)',
                  border: '1px solid #6366f1',
                  color: '#fff',
                  borderRadius: '6px',
                  fontSize: '14px',
                  boxSizing: 'border-box',
                  opacity: (isLocked || !stems[0] || !stems[1]) ? 0.5 : 1,
                  cursor: (isLocked || !stems[0] || !stems[1]) ? 'not-allowed' : 'text'
                }}
              />
            </div>

            {/* Key Input */}
            <div style={{ marginBottom: '15px' }}>
              <label style={{ fontSize: '12px', color: '#999', marginBottom: '5px', display: 'block' }}>
                🎼 Target Key (for transposition)
              </label>
              <select
                value={targetKey || ''}
                onChange={(e) => handleTargetKeyChange(e.target.value)}
                disabled={isLocked || !stems[0] || !stems[1]}
                style={{
                  width: '100%',
                  padding: '8px',
                  background: '#1a1f3a',
                  border: '1px solid #6366f1',
                  color: '#fff',
                  borderRadius: '6px',
                  fontSize: '14px',
                  boxSizing: 'border-box',
                  colorScheme: 'dark',
                  opacity: (isLocked || !stems[0] || !stems[1]) ? 0.5 : 1,
                  cursor: (isLocked || !stems[0] || !stems[1]) ? 'not-allowed' : 'pointer'
                }}
              >
                <option value="" style={{ background: '#1a1f3a', color: '#fff' }}>No transposition</option>
                {KEYS.map(k => <option key={k} value={k} style={{ background: '#1a1f3a', color: '#fff' }}>{k}</option>)}
              </select>
            </div>

            {/* Unified Process Button */}
            <button
              onClick={handleProcessAllChanges}
              disabled={isLocked || !stems[0] || !stems[1] || (!bpmChanged && !keyChanged)}
              style={{
                width: '100%',
                padding: '10px 16px',
                background: (isLocked || !stems[0] || !stems[1] || (!bpmChanged && !keyChanged)) ? '#444' : '#6366f1',
                border: 'none',
                color: '#fff',
                borderRadius: '6px',
                cursor: (isLocked || !stems[0] || !stems[1] || (!bpmChanged && !keyChanged)) ? 'not-allowed' : 'pointer',
                fontWeight: 'bold',
                fontSize: '14px',
                opacity: (isLocked || !stems[0] || !stems[1] || (!bpmChanged && !keyChanged)) ? 0.5 : 1
              }}
            >
              🎯 Process All Changes
            </button>

            {/* Processing Progress Bar */}
            {isProcessing && (
              <>
                <div style={{
                  margin: '10px 0',
                  background: 'rgba(99, 102, 241, 0.1)',
                  border: '1px solid #6366f1',
                  borderRadius: '4px',
                  overflow: 'hidden',
                  height: '24px'
                }}>
                  <div style={{
                    width: `${processingProgress}%`,
                    height: '100%',
                    background: 'linear-gradient(90deg, #6366f1, #8b5cf6)',
                    transition: 'width 0.3s ease',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    color: '#fff',
                    fontSize: '12px',
                    fontWeight: 'bold'
                  }}>
                    {processingProgress < 100 && `${Math.round(processingProgress)}%`}
                    {processingProgress === 100 && '✅ Complete'}
                  </div>
                </div>
              </>
            )}

            <p style={{ margin: '10px 0 0 0', color: '#999', fontSize: '12px' }}>
              💡 {(() => {
                const parts = [];
                if (targetBpm) parts.push(`aligned to ${targetBpm} BPM`);
                if (targetKey) parts.push(`transposed to ${targetKey}`);
                return parts.length > 0 ? `Songs will be ${parts.join(' and ')}` : 'Set target BPM or key and click Process';
              })()}
            </p>

            {/* Beatmatch Status */}
            {(targetBpm || (stems[0] && stems[1])) && (beatmatchStatus[0] || beatmatchStatus[1]) && (
              <div style={{
                background: 'rgba(139, 92, 246, 0.1)',
                border: '1px solid rgba(139, 92, 246, 0.3)',
                padding: '12px',
                borderRadius: '8px',
                fontSize: '12px'
              }}>
                <div style={{ display: 'flex', gap: '20px', flexWrap: 'wrap' }}>
                  {[0, 1].map(slot => {
                    if (!stems[slot] || !beatmatchStatus[slot]) return null;
                    const status = beatmatchStatus[slot];
                    const statusEmoji = status === 'processing' ? '⏳' : status === 'done' ? '✅' : '❌';
                    const sourceBpm = getEffectiveBpm(slot);
                    const sourceKey = getEffectiveKey(slot);
                    const targetBpmForSlot = targetBpm || (slot === 1 ? getEffectiveBpm(0) : null);
                    const targetKeyForSlot = targetKey || sourceKey;

                    return (
                      <div key={slot} style={{ color: '#aaa' }}>
                        <strong style={{ color: '#8b5cf6' }}>{metadata[slot]?.filename?.replace(/\.[^/.]+$/, '')}</strong>
                        <br />
                        {sourceBpm} → {targetBpmForSlot} BPM {sourceKey && targetKeyForSlot && `| ${sourceKey} → ${targetKeyForSlot}`} <span style={{ marginLeft: '8px' }}>{statusEmoji} {status === 'processing' ? 'Processing...' : status === 'done' ? 'Ready' : 'Failed'}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        ) : null}

        {/* Processing Status & Recommendations -- shown as soon as this
            processing window is available (both songs have stems), not only
            after a target BPM/key has already been picked, so the
            recommendation can actually inform that choice. */}
        {stems[0] && stems[1] && (
          <div style={{
            background: 'rgba(99, 102, 241, 0.05)',
            border: '1px solid rgba(99, 102, 241, 0.2)',
            padding: '20px',
            borderRadius: '12px',
            marginTop: '20px'
          }}>
            <label style={{ display: 'block', fontSize: '14px', fontWeight: 'bold', color: '#ccc', marginBottom: '10px' }}>
              💡 Recommended Keys (Camelot Wheel)
            </label>

            {ownCompatibility !== null && ownCompatibility <= 1 && (
              <div style={{
                background: 'rgba(34, 197, 94, 0.12)',
                border: '1px solid rgba(34, 197, 94, 0.4)',
                borderRadius: '6px',
                padding: '8px 12px',
                marginBottom: '12px',
                fontSize: '12px',
                color: '#86efac'
              }}>
                ✅ No change needed -- Song 1 [{camelotCode(getEffectiveKey(0), getEffectiveScale(0))}] and Song 2 [{camelotCode(getEffectiveKey(1), getEffectiveScale(1))}] are already
                {ownCompatibility === 0 ? ' in the same key.' : ' a compatible pair (relative or adjacent on the Camelot wheel).'}
              </div>
            )}

            <p style={{ margin: '0 0 12px 0', fontSize: '11px', color: '#888' }}>
              Ranked by semitone distance first (0, ±1, ±2...), then by harmonic compatibility. Smaller shifts are preferred.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: '15px' }}>
              {keyRecommendations.length === 0 ? (
                <span style={{ fontSize: '12px', color: '#888' }}>No recommendation available yet.</span>
              ) : keyRecommendations.map(rec => (
                <button
                  key={rec.key}
                  onClick={() => handleTargetKeyChange(rec.key)}
                  disabled={isLocked}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    gap: '10px',
                    background: targetKey === rec.key ? 'rgba(139, 92, 246, 0.25)' : 'transparent',
                    color: '#e5e7eb',
                    border: targetKey === rec.key ? '1px solid #8b5cf6' : '1px solid rgba(139, 92, 246, 0.3)',
                    padding: '6px 12px',
                    borderRadius: '6px',
                    cursor: isLocked ? 'not-allowed' : 'pointer',
                    fontSize: '12px',
                    textAlign: 'left',
                    opacity: isLocked ? 0.5 : 1
                  }}
                >
                  <span>{rec.emoji} <strong>{rec.key}</strong> <span style={{ color: '#999' }}>({rec.label})</span></span>
                  <span style={{ color: '#999', fontSize: '11px' }}>
                    {[0, 1].filter(slot => stems[slot]).map(slot => {
                      const shift = getSemitoneShift(getEffectiveKey(slot), rec.key);
                      const sign = shift > 0 ? '+' : '';
                      const camelot = slot === 0 ? rec.camelot1 : rec.camelot2;
                      return `Song ${slot + 1}: ${camelot ?? '?'} (${sign}${shift} st)`;
                    }).join('  ·  ')}
                  </span>
                </button>
              ))}
            </div>

            {/* Processing Preview -- only meaningful once a target is set */}
            {(targetBpm || targetKey) && (
            <div style={{
              background: 'rgba(99, 102, 241, 0.1)',
              border: '1px solid rgba(99, 102, 241, 0.2)',
              padding: '12px',
              borderRadius: '6px'
            }}>
              {[0, 1].map(slot => {
                if (!stems[slot]) return null;
                const sourceBpm = getEffectiveBpm(slot);
                const sourceKey = getEffectiveKey(slot);
                const keyShift = targetKey ? getSemitoneShift(sourceKey, targetKey) : 0;
                const keyDirection = keyShift > 0 ? '↑' : keyShift < 0 ? '↓' : '=';
                const keyShiftLabel = targetKey && keyShift !== 0 ? ` (${keyShift > 0 ? '+' : ''}${keyShift} semitone${Math.abs(keyShift) === 1 ? '' : 's'})` : '';

                return (
                  <div key={slot} style={{ marginBottom: slot === 0 ? '8px' : '0', color: '#aaa', fontSize: '12px' }}>
                    <strong style={{ color: '#8b5cf6' }}>{metadata[slot]?.filename?.replace(/\.[^/.]+$/, '')}</strong><br/>
                    {targetBpm && `${sourceBpm} → ${targetBpm} BPM`}
                    {targetBpm && targetKey && ' | '}
                    {targetKey && `${sourceKey} → ${targetKey} ${keyDirection}${keyShiftLabel}`}
                  </div>
                );
              })}
            </div>
            )}
          </div>
        )}

        {/* Downloads */}
        {(stems[0] || stems[1]) && (
          <div style={{
            background: 'rgba(99, 102, 241, 0.05)',
            border: '1px solid rgba(99, 102, 241, 0.2)',
            padding: '20px',
            borderRadius: '12px',
            marginTop: '20px'
          }}>
            <h4 style={{ margin: '0 0 15px 0', color: '#6366f1' }}>📥 Downloads</h4>
            <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap', justifyContent: 'center' }}>
              {/* Download Original (or Aligned, if that's what was actually
                  separated) Stems -- once a song is processed with 'align',
                  the stems here ARE the aligned ones; there's no separate
                  unaligned copy unless the button below is used. */}
              <button
                onClick={() => {
                  const timestamps = metadata.map(m => m?.timestamp).filter(Boolean);
                  if (!timestamps.length) {
                    alert('No stems to download');
                    return;
                  }
                  // Label with the CURRENT actual bpm/key, not the fixed
                  // originally-detected one -- this download's content is
                  // whatever the latest separated stems are (post any
                  // BPM/Key reprocessing), so the filename should match.
                  const metadataList = metadata.map((m, i) => m ? {
                    filename: m.filename,
                    bpm: getCurrentBpm(i) ?? generateBpmLabel(m, false),
                    key: getCurrentKey(i) ?? m.key
                  } : null);

                  handleDownload('original', '/api/download-stems-zip', {
                    timestamps,
                    metadata: metadataList,
                    include_original: true,
                    include_processed: false
                  });
                }}
                disabled={downloadingKey !== null}
                style={{
                  background: 'rgba(99, 102, 241, 0.2)',
                  color: '#a78bfa',
                  border: '1px solid #6366f1',
                  padding: '10px 16px',
                  borderRadius: '6px',
                  cursor: downloadingKey !== null ? 'not-allowed' : 'pointer',
                  opacity: downloadingKey !== null ? 0.5 : 1,
                  fontSize: '12px',
                  fontWeight: 'bold'
                }}
              >
                {downloadingKey === 'original' ? '⏳ Preparing ZIP...' : `📦 ${originalStemsLabel}`}
              </button>

              {/* Download Unaligned Originals -- on request only: re-runs
                  Demucs on the pre-alignment WAV for whichever song(s) used
                  'align', since that source is never separated automatically. */}
              {anySlotAligned && (
                <button
                  onClick={() => {
                    const wavFilenames = metadata.map(m => m?.mode === 'align' ? m.unalignedWavFilename : null);
                    if (!wavFilenames.some(Boolean)) {
                      alert('No unaligned originals available');
                      return;
                    }
                    const metadataList = metadata.map(m => m ? {
                      filename: m.filename,
                      bpm: generateBpmLabel(m, false),
                      key: m.key
                    } : null);

                    handleDownload('unaligned', '/api/download-unaligned-stems', {
                      wav_filenames: wavFilenames,
                      metadata: metadataList
                    });
                  }}
                  disabled={downloadingKey !== null}
                  style={{
                    background: 'rgba(99, 102, 241, 0.2)',
                    color: '#a78bfa',
                    border: '1px dashed #6366f1',
                    padding: '10px 16px',
                    borderRadius: '6px',
                    cursor: downloadingKey !== null ? 'not-allowed' : 'pointer',
                    opacity: downloadingKey !== null ? 0.5 : 1,
                    fontSize: '12px',
                    fontWeight: 'bold'
                  }}
                >
                  {downloadingKey === 'unaligned' ? '⏳ Separating + preparing ZIP...' : '📦 Unaligned Originals'}
                </button>
              )}

              {/* Download Processed Stems */}
              {(transposedStems[0] || transposedStems[1] || beatmatchedStems[0] || beatmatchedStems[1]) && (
                <button
                  onClick={() => {
                    const timestamps = metadata.map(m => m?.timestamp).filter(Boolean);
                    if (!timestamps.length) {
                      alert('No stems to download');
                      return;
                    }
                    const metadataList = metadata.map(m => m ? {
                      filename: m.filename,
                      bpm: generateBpmLabel(m, true),
                      key: targetKey || m.key
                    } : null);

                    handleDownload('processed', '/api/download-stems-zip', {
                      timestamps,
                      metadata: metadataList,
                      include_original: false,
                      include_processed: true
                    });
                  }}
                  disabled={downloadingKey !== null}
                  style={{
                    background: 'rgba(139, 92, 246, 0.2)',
                    color: '#c4b5fd',
                    border: '1px solid #8b5cf6',
                    padding: '10px 16px',
                    borderRadius: '6px',
                    cursor: downloadingKey !== null ? 'not-allowed' : 'pointer',
                    opacity: downloadingKey !== null ? 0.5 : 1,
                    fontSize: '12px',
                    fontWeight: 'bold'
                  }}
                >
                  {downloadingKey === 'processed' ? '⏳ Preparing ZIP...' : '📦 Processed Stems'}
                </button>
              )}

              {/* Download Final Mix */}
              <button
                onClick={() => {
                  const timestamps = metadata.map(m => m?.timestamp).filter(Boolean);
                  if (!timestamps.length) {
                    alert('No stems to mix');
                    return;
                  }
                  const metadataList = metadata.map(m => m ? {
                    filename: m.filename,
                    bpm: generateBpmLabel(m, true),
                    key: targetKey || m.key,
                    beat_anchor: m.beat_anchor
                  } : null);

                  handleDownload('final', '/api/render-final-mix', {
                    timestamps,
                    metadata: metadataList,
                    volumes,
                    crossfader,
                    beat_offsets: [0, beatOffset],
                    target_bpm: targetBpm
                  });
                }}
                disabled={downloadingKey !== null}
                style={{
                  background: 'rgba(34, 197, 94, 0.2)',
                  color: '#86efac',
                  border: '1px solid #22c55e',
                  padding: '10px 16px',
                  borderRadius: '6px',
                  cursor: downloadingKey !== null ? 'not-allowed' : 'pointer',
                  opacity: downloadingKey !== null ? 0.5 : 1,
                  fontSize: '12px',
                  fontWeight: 'bold'
                }}
              >
                {downloadingKey === 'final' ? '⏳ Rendering...' : '🎵 Final Mix (WAV)'}
              </button>
            </div>
            <p style={{ margin: '12px 0 0 0', color: '#999', fontSize: '12px' }}>
              💡 All downloads are WAV with an embedded ACID chunk (BPM/key DAWs like FL Studio can auto-detect on import): songname-[BPM]-[KEY]-[stem/mix].wav
            </p>
          </div>
        )}

        {/* Audio Stats & Cleanup */}
        <div style={{
          marginTop: '40px',
          paddingTop: '20px',
          borderTop: '1px solid rgba(99, 102, 241, 0.2)',
          textAlign: 'center'
        }}>
          <div style={{
            marginBottom: '15px',
            fontSize: '13px',
            color: '#aaa'
          }}>
            <strong>💾 Generated Files:</strong> {audioStats.file_count} files ({audioStats.total_size_formatted})
          </div>

          <button
            onClick={handleCleanup}
            style={{
              background: 'rgba(239, 68, 68, 0.2)',
              color: '#fca5a5',
              border: '1px solid #ef4444',
              padding: '10px 20px',
              borderRadius: '6px',
              cursor: 'pointer',
              fontSize: '13px',
              fontWeight: 'bold',
              transition: 'all 0.2s'
            }}
            onMouseEnter={(e) => e.target.style.background = 'rgba(239, 68, 68, 0.3)'}
            onMouseLeave={(e) => e.target.style.background = 'rgba(239, 68, 68, 0.2)'}
          >
            🗑️ Clean and Reset
          </button>
        </div>
      </div>
    </div>
  );
}
