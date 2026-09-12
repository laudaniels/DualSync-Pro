import React, { useState, useRef, useEffect, useCallback } from 'react';
import Waveform from './Waveform';
import SongMixer from './SongMixer';
import { stemNames, stemLabels } from './stemConstants';
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
  const [beatOffset, setBeatOffset] = useState(0); // 0-8 beats for Song 2
  const [beatOffsetDisplay, setBeatOffsetDisplay] = useState(0); // Fine-tuned display value
  const [isSnappedToBeat, setIsSnappedToBeat] = useState(false); // Visual feedback for snap
  const [kickWaveforms, setKickWaveforms] = useState(null); // Kick drum waveforms for display
  const [selectedStemsForWaveform, setSelectedStemsForWaveform] = useState(['kick']); // Which stems to display in waveform
  const [waveformZoom, setWaveformZoom] = useState(10); // Waveform zoom level in seconds

  // Volume states for each song (with split drums)
  const [volumes, setVolumes] = useState({
    0: { vocals: 1.0, kick: 1.0, snare: 1.0, hihat: 1.0, tom: 1.0, bass: 1.0, other: 1.0 },
    1: { vocals: 1.0, kick: 1.0, snare: 1.0, hihat: 1.0, tom: 1.0, bass: 1.0, other: 1.0 }
  });

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

  // Magnetic snap behavior for beat offset
  React.useEffect(() => {
    // Check if within 0.1 of a whole beat
    const nearestBeat = Math.round(beatOffsetDisplay);
    const snapThreshold = 0.15;
    const distanceToNearestBeat = Math.abs(beatOffsetDisplay - nearestBeat);

    if (distanceToNearestBeat < snapThreshold && nearestBeat >= 0 && nearestBeat <= 8) {
      // Snap to beat
      setBeatOffset(nearestBeat);
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
      // Not near a beat, just update beatOffset directly
      setBeatOffset(parseFloat(beatOffsetDisplay.toFixed(1)));
      setIsSnappedToBeat(false);
    }

    return () => {
      if (beatSnapTimeoutRef.current) {
        clearTimeout(beatSnapTimeoutRef.current);
      }
    };
  }, [beatOffsetDisplay]);

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

  // Auto-scroll logs to latest message
  React.useEffect(() => {
    if (logsEndRef.current) {
      logsEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [processingLogs]);

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
          mode
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
        newMetadata[slot] = { ...data, timestamp: data.timestamp || Date.now().toString() };
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
        updated[slot] = playerRef.current.isSlotReady(slot, stemNames);
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
  }, [pendingSong, fetchStats]);


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
      playerRef.current.play(stemNames);
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

    playerRef.current.seek(newTime, stemNames);
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
    }, 100);

    return () => clearInterval(interval);
  }, [playing]);

  // Update beat offset delay in real-time
  useEffect(() => {
    const song2BpmStr = metadata[1]?.bpm ? String(metadata[1].bpm) : '120';
    const song2Bpm = parseFloat(song2BpmStr.split('-')[0]) || 120;
    playerRef.current.setBeatOffset(beatOffset, song2Bpm);
  }, [beatOffset, metadata]);

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

  // Get recommended target key (best compromise between both songs)
  const getRecommendedKey = () => {
    const key0 = getEffectiveKey(0);
    const key1 = getEffectiveKey(1);

    if (!key0 || !key1 || !stems[0] || !stems[1]) return null;

    const keyToIndex = {};
    KEYS.forEach((k, i) => keyToIndex[k] = i);

    const idx0 = keyToIndex[key0];
    const idx1 = keyToIndex[key1];

    let bestKey = null;
    let bestScore = Infinity;

    // Find key that minimizes maximum transposition (most balanced)
    // Example: E + F# → F is better (both ±1) than E (0 + 2) or F# (1 + 0)
    KEYS.forEach((testKey, testIdx) => {
      const shift0 = Math.abs(getSemitoneShift(key0, testKey));
      const shift1 = Math.abs(getSemitoneShift(key1, testKey));
      const score = Math.max(shift0, shift1); // Minimize maximum individual shift

      if (score < bestScore) {
        bestScore = score;
        bestKey = testKey;
      }
    });

    return bestKey;
  };

  const getEffectiveBpm = (slot) => {
    if (overrideBpm[slot] !== null) return overrideBpm[slot];
    // Prioritize measured_bpm (after beatmatching) over detected bpm
    return metadata[slot]?.measured_bpm || metadata[slot]?.bpm;
  };
  const getEffectiveKey = (slot) => overrideKey[slot] !== null ? overrideKey[slot] : metadata[slot]?.key;

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
          slot: slot
        })
      });

      clearInterval(statusPoller);
      const data = await response.json();

      if (data.processed_stems) {
        // Store measured BPM in metadata for accurate file naming
        setMetadata(prevMetadata => {
          const newMetadata = [...prevMetadata];
          if (newMetadata[slot] && data.measured_bpm) {
            newMetadata[slot] = {
              ...newMetadata[slot],
              measured_bpm: data.measured_bpm,
              source_bpm: data.source_bpm,
              target_bpm: data.target_bpm
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

      // Update metadata with new key if transposed
      if (needsKeyChange) {
        setMetadata(prev => {
          const updated = [...prev];
          for (let i = 0; i < updated.length; i++) {
            if (updated[i]) {
              updated[i] = { ...updated[i], key: targetKey };
            }
          }
          return updated;
        });
      }
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

  // Cleanup all audio files
  const handleCleanup = async () => {
    if (!confirm('🗑️ Delete all generated audio files? This cannot be undone.')) return;

    try {
      const response = await fetch('/api/cleanup', { method: 'POST' });
      const data = await response.json();

      if (response.ok) {
        alert('✅ Cleanup complete! All audio files deleted.');
        // Reset state
        playerRef.current.reset();
        setStems([null, null]);
        setMetadata([null, null]);
        setLoading([false, false]);
        setAudioReady([false, false]);
        setPendingSong([null, null]);
        setPlaying(false);
        setCurrentTime(0);
        setDuration(0);
        setCrossfader(50);
        setAudioStats({ file_count: 0, total_size_formatted: '0 MB' });
      } else {
        alert(`❌ Cleanup failed: ${data.error}`);
      }
    } catch (err) {
      console.error('Cleanup error:', err);
      alert(`❌ Error: ${err.message}`);
    }
  };

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
              audioReady={audioReady[slot]}
              pendingSong={pendingSong[slot]}
              processingStage={processingStage[slot]}
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
              {stemNames.map(stem => (
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
                  <span style={{ fontSize: '12px' }}>{stemLabels[stem]}</span>
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
                song2Bpm={metadata[1]?.bpm ? parseFloat(String(metadata[1].bpm).split('-')[0]) : 120}
                song1Bpm={metadata[0]?.bpm ? parseFloat(String(metadata[0].bpm).split('-')[0]) : 120}
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

            {/* Beat Offset for Song 2 with Magnetic Snap */}
            <div className="crossfader-section" style={{ marginTop: '15px' }}>
              <label>🎵 Beat Offset (Song 2) — Fine-tune + Snap to Beats</label>
              <div className="crossfader-labels">
                <span>Sync</span>
                <span>Offset</span>
                <span>+8 beats</span>
              </div>
              <div style={{ position: 'relative' }}>
                <input
                  type="range"
                  min="0"
                  max="8"
                  step="0.1"
                  value={beatOffsetDisplay}
                  onChange={(e) => setBeatOffsetDisplay(parseFloat(e.target.value))}
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
                  `🎯 Snapped: +${beatOffset} beat${beatOffset !== 1 ? 's' : ''} (fine-tune: ${beatOffsetDisplay.toFixed(1)})`
                ) : beatOffsetDisplay === 0 ? (
                  '✓ Sync (no offset)'
                ) : (
                  `⚙️ Fine-tuning: +${beatOffsetDisplay.toFixed(1)} beats`
                )}
              </div>
            </div>

            {/* Stems Version Info - Directly under Controls */}
            {(stems[0] || stems[1]) && (
              <div style={{
                background: 'rgba(100, 116, 139, 0.2)',
                border: '1px solid rgba(100, 116, 139, 0.4)',
                padding: '8px 12px',
                borderRadius: '6px',
                fontSize: '11px',
                color: '#94a3b8',
                marginTop: '10px'
              }}>
                📦 Current stems: {stems[0] && `Song 1 [${getEffectiveKey(0)} ${getEffectiveBpm(0)} BPM]`} {stems[1] && `+ Song 2 [${getEffectiveKey(1)} ${getEffectiveBpm(1)} BPM]`}
                {(targetBpm || targetKey) && !isLocked && (
                  <>
                    <br />
                    🎯 Will process to: {stems[0] && `Song 1 [${targetKey || getEffectiveKey(0)} ${targetBpm || getEffectiveBpm(0)} BPM]`} {stems[1] && `+ Song 2 [${targetKey || getEffectiveKey(1)} ${targetBpm || getEffectiveBpm(1)} BPM]`}
                  </>
                )}
              </div>
            )}
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
            <label style={{ display: 'block', fontSize: '14px', fontWeight: 'bold', color: '#ccc', marginBottom: '15px' }}>
              ⚙️ Processing (BPM & Key)
            </label>

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
                disabled={isLocked}
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
                  opacity: isLocked ? 0.5 : 1,
                  cursor: isLocked ? 'not-allowed' : 'pointer'
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

        {/* Processing Status & Recommendations */}
        {stems[0] && stems[1] && (targetBpm || targetKey) && (
          <div style={{
            background: 'rgba(99, 102, 241, 0.05)',
            border: '1px solid rgba(99, 102, 241, 0.2)',
            padding: '20px',
            borderRadius: '12px',
            marginTop: '20px'
          }}>
            <label style={{ display: 'block', fontSize: '14px', fontWeight: 'bold', color: '#ccc', marginBottom: '15px' }}>
              💡 Recommended Key: <button
                onClick={() => handleTargetKeyChange(getRecommendedKey())}
                style={{
                  background: 'transparent',
                  color: '#a78bfa',
                  border: '1px solid #8b5cf6',
                  padding: '4px 10px',
                  borderRadius: '4px',
                  cursor: 'pointer',
                  fontSize: '11px',
                  fontWeight: 'bold',
                  marginLeft: '10px'
                }}
              >
                {getRecommendedKey()}
              </button>
            </label>

            {/* Processing Preview */}
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

                return (
                  <div key={slot} style={{ marginBottom: slot === 0 ? '8px' : '0', color: '#aaa', fontSize: '12px' }}>
                    <strong style={{ color: '#8b5cf6' }}>{metadata[slot]?.filename?.replace(/\.[^/.]+$/, '')}</strong><br/>
                    {targetBpm && `${sourceBpm} → ${targetBpm} BPM`}
                    {targetBpm && targetKey && ' | '}
                    {targetKey && `${sourceKey} → ${targetKey} ${keyDirection}`}
                  </div>
                );
              })}
            </div>
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
              {/* Download Original Stems */}
              <button
                onClick={() => {
                  const timestamps = metadata.map(m => m?.timestamp).filter(Boolean);
                  if (!timestamps.length) {
                    alert('No stems to download');
                    return;
                  }
                  const metadataList = metadata.map(m => m ? {
                    filename: m.filename,
                    bpm: generateBpmLabel(m, false),
                    key: m.key
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
                {downloadingKey === 'original' ? '⏳ Preparing ZIP...' : '📦 Original Stems'}
              </button>

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
                    key: targetKey || m.key
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
                {downloadingKey === 'final' ? '⏳ Rendering...' : '🎵 Final Mix (FLAC)'}
              </button>
            </div>
            <p style={{ margin: '12px 0 0 0', color: '#999', fontSize: '12px' }}>
              💡 All downloads use FLAC format with metadata tags: songname-[BPM]-[KEY]-[stem/mix].flac
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
            🗑️ Cleanup Audio Files
          </button>
        </div>
      </div>
    </div>
  );
}
