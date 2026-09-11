import React, { useState, useRef, useEffect } from 'react';
import StemLoader from './StemLoader';
import Waveform from './Waveform';
import '../styles/DualMixer.css';

export default function DualMixer() {
  const [stems, setStems] = useState([null, null]);
  const [metadata, setMetadata] = useState([null, null]);
  const [loading, setLoading] = useState([false, false]);
  const [error, setError] = useState('');
  const [playing, setPlaying] = useState(false);
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

  // Web Audio API for waveform visualization and beat offset
  const audioContextRef = useRef(null);
  const analyserRef = useRef(null);
  const audioSourcesRef = useRef({});
  const delayNodeRef = useRef(null); // For Song 2 beat offset
  const [beatOffset, setBeatOffset] = useState(0); // 0-8 beats for Song 2
  const [kickWaveforms, setKickWaveforms] = useState(null); // Kick drum waveforms for display

  // Stem names now include split drums (drums → kick, snare, hihat, tom)
  const stemNames = ['vocals', 'kick', 'snare', 'hihat', 'tom', 'bass', 'other'];
  const stemLabels = {
    vocals: '🎤 Vocals',
    kick: '🔊 Kick',
    snare: '🥁 Snare',
    hihat: '⚡ Hi-Hat',
    tom: '🔔 Tom',
    bass: '🎸 Bass',
    other: '🎹 Other'
  };

  // Audio refs for each song's stems (with split drums)
  const audioRefsRef = useRef({
    0: { vocals: useRef(null), kick: useRef(null), snare: useRef(null), hihat: useRef(null), tom: useRef(null), bass: useRef(null), other: useRef(null) },
    1: { vocals: useRef(null), kick: useRef(null), snare: useRef(null), hihat: useRef(null), tom: useRef(null), bass: useRef(null), other: useRef(null) }
  });

  // Volume states for each song (with split drums)
  const [volumes, setVolumes] = useState({
    0: { vocals: 1.0, kick: 1.0, snare: 1.0, hihat: 1.0, tom: 1.0, bass: 1.0, other: 1.0 },
    1: { vocals: 1.0, kick: 1.0, snare: 1.0, hihat: 1.0, tom: 1.0, bass: 1.0, other: 1.0 }
  });

  // Crossfader: 0 = song1 only, 50 = both, 100 = song2 only
  const [crossfader, setCrossfader] = useState(50);

  // Fetch audio stats
  const fetchStats = async () => {
    try {
      const response = await fetch('/api/audio-stats');
      const data = await response.json();
      setAudioStats(data);
    } catch (err) {
      console.error('Stats fetch error:', err);
    }
  };

  // Fetch stats on component mount
  React.useEffect(() => {
    fetchStats();
  }, []);

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

  // Handle stem upload for a song slot
  const handleStemsLoaded = async (file, slot) => {
    const newLoading = [...loading];
    newLoading[slot] = true;
    setLoading(newLoading);
    setError('');

    // Show filename immediately while processing
    const newMetadata = [...metadata];
    newMetadata[slot] = { filename: file.name };
    setMetadata(newMetadata);

    console.log(`📥 Song ${slot + 1} upload started`);

    try {
      const formData = new FormData();
      formData.append('file', file);

      console.log(`🔄 Uploading Song ${slot + 1} for stem separation...`);
      const response = await fetch('/api/separate-stems', {
        method: 'POST',
        body: formData
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

      // Set audio sources
      console.log(`🎵 Song ${slot + 1} stems received:`, Object.keys(data.stems));
      Object.entries(data.stems).forEach(([stemName, url]) => {
        if (audioRefsRef.current[slot][stemName]?.current) {
          audioRefsRef.current[slot][stemName].current.src = url;
          audioRefsRef.current[slot][stemName].current.load();
          console.log(`✅ Loaded ${stemName} for Song ${slot + 1}: ${url}`);
        } else {
          console.warn(`⚠️ No audio ref for ${stemName} on Song ${slot + 1}`);
        }
      });

      // Refresh stats
      fetchStats();
    } catch (err) {
      console.error(`❌ Error:`, err);
      setError(`Error loading Song ${slot + 1}: ${err.message}`);
    } finally {
      setLoading(prevLoading => {
        const updated = [...prevLoading];
        updated[slot] = false;
        return updated;
      });
    }
  };

  // Play/pause both songs
  const togglePlayback = () => {
    if (playing) {
      // Stop all
      [0, 1].forEach(slot => {
        stemNames.forEach(stem => {
          if (audioRefsRef.current[slot][stem]?.current) {
            audioRefsRef.current[slot][stem].current.pause();
            audioRefsRef.current[slot][stem].current.currentTime = 0;
          }
        });
      });
      setPlaying(false);
      setCurrentTime(0);
    } else {
      // Play all loaded stems
      const crossfadePercent = crossfader / 100;
      const song1Volume = 1 - crossfadePercent; // 1 at 0%, 0 at 100%
      const song2Volume = crossfadePercent; // 0 at 0%, 1 at 100%

      [0, 1].forEach(slot => {
        let loadedCount = 0;
        stemNames.forEach(stem => {
          const audioEl = audioRefsRef.current[slot][stem]?.current;
          if (audioEl && audioEl.src) {
            // Apply crossfader
            const masterVol = slot === 0 ? song1Volume : song2Volume;
            const stemVol = volumes[slot]?.[stem] ?? 1.0;
            audioEl.volume = masterVol * stemVol;
            audioEl.play().catch(e => console.error(`Play error (Song ${slot + 1} ${stem}):`, e));
            loadedCount++;
          } else {
            console.warn(`⚠️ Song ${slot + 1} ${stem}: no src or element missing`);
          }
        });
        console.log(`▶️ Playing Song ${slot + 1}: ${loadedCount}/${stemNames.length} stems loaded`);
      });
      setPlaying(true);
    }
  };

  // Update volume
  const handleVolumeChange = (slot, stem, value) => {
    setVolumes(prev => ({
      ...prev,
      [slot]: { ...prev[slot], [stem]: value }
    }));

    if (playing && audioRefsRef.current[slot][stem]?.current) {
      const crossfadePercent = crossfader / 100;
      const masterVol = slot === 0 ? (1 - crossfadePercent) : crossfadePercent;
      audioRefsRef.current[slot][stem].current.volume = masterVol * value;
    }
  };

  // Update crossfader
  const handleCrossfaderChange = (value) => {
    setCrossfader(value);

    if (playing) {
      const crossfadePercent = value / 100;
      const song1Volume = 1 - crossfadePercent;
      const song2Volume = crossfadePercent;
      console.log(`🎚️ Crossfader: Song1=${song1Volume.toFixed(2)}, Song2=${song2Volume.toFixed(2)}`);

      [0, 1].forEach(slot => {
        const masterVol = slot === 0 ? song1Volume : song2Volume;
        console.log(`   Slot ${slot}: masterVol=${masterVol.toFixed(2)}`);
        stemNames.forEach(stem => {
          const audioEl = audioRefsRef.current[slot][stem]?.current;
          if (audioEl && audioEl.src) {
            const stemVol = volumes[slot]?.[stem] ?? 1.0;
            audioEl.volume = masterVol * stemVol;
          }
        });
      });
    }
  };

  const handleSeek = (e) => {
    if (duration === 0) return;

    // Get click position relative to progress bar
    const rect = e.currentTarget.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const percentage = clickX / rect.width;
    const newTime = percentage * duration;

    // Seek all audio elements
    stemNames.forEach(stem => {
      [0, 1].forEach(slot => {
        const audioEl = audioRefsRef.current[slot][stem]?.current;
        if (audioEl) {
          audioEl.currentTime = newTime;
        }
      });
    });

    setCurrentTime(newTime);
  };

  // Track progress
  useEffect(() => {
    if (!playing) return;

    const interval = setInterval(() => {
      if (audioRefsRef.current[0].vocals?.current) {
        const ct = audioRefsRef.current[0].vocals.current.currentTime;
        const dur = audioRefsRef.current[0].vocals.current.duration || 0;
        setCurrentTime(ct);
        setDuration(dur);

        if (ct >= dur - 0.1) {
          // Auto-loop: restart playback
          setCurrentTime(0);
          [0, 1].forEach(slot => {
            stemNames.forEach(stem => {
              if (audioRefsRef.current[slot][stem]?.current) {
                audioRefsRef.current[slot][stem].current.currentTime = 0;
                audioRefsRef.current[slot][stem].current.play().catch(() => {});
              }
            });
          });
        }
      }
    }, 100);

    return () => clearInterval(interval);
  }, [playing]);

  // Initialize audio context and analyser (run once on mount)
  useEffect(() => {
    const initAudioContext = () => {
      try {
        if (audioContextRef.current) return; // Already initialized

        audioContextRef.current = new (window.AudioContext || window.webkitAudioContext)();
        console.log('🎵 Audio context created:', audioContextRef.current.state);

        analyserRef.current = audioContextRef.current.createAnalyser();
        analyserRef.current.fftSize = 512;
        analyserRef.current.smoothingTimeConstant = 0.8;

        // Create and connect gain node
        const gainNode = audioContextRef.current.createGain();
        gainNode.gain.value = 1.0;
        analyserRef.current.connect(gainNode);
        gainNode.connect(audioContextRef.current.destination);

        console.log('🎵 Analyser initialized');
      } catch (e) {
        console.error('Audio context init failed:', e);
      }
    };

    // Initialize on first user interaction
    const handleUserInteraction = () => {
      initAudioContext();
      document.removeEventListener('click', handleUserInteraction);
      document.removeEventListener('keypress', handleUserInteraction);
    };

    document.addEventListener('click', handleUserInteraction);
    document.addEventListener('keypress', handleUserInteraction);

    return () => {
      document.removeEventListener('click', handleUserInteraction);
      document.removeEventListener('keypress', handleUserInteraction);
    };
  }, []);

  // Connect audio elements to analyser when they're available
  useEffect(() => {
    if (!audioContextRef.current || !analyserRef.current) return;

    const audioContext = audioContextRef.current;

    // Create delay node for Song 2 beat offset
    if (!delayNodeRef.current) {
      delayNodeRef.current = audioContext.createDelay(8); // Max 8 seconds
      delayNodeRef.current.connect(audioContext.destination);
    }

    const connectSources = () => {
      let connected = false;

      for (let slot = 0; slot < 2; slot++) {
        for (let stem of stemNames) {
          const audioEl = audioRefsRef.current[slot]?.[stem]?.current;
          const key = `${slot}-${stem}`;

          if (audioEl?.src && !audioSourcesRef.current[key]) {
            try {
              if (audioContext.state === 'suspended') {
                audioContext.resume();
              }

              const source = audioContext.createMediaElementAudioSource(audioEl);

              // Route Song 2 through delay node, Song 1 directly to destination
              if (slot === 1) {
                // Song 2: route through delay → destination
                source.connect(delayNodeRef.current);
                source.connect(analyserRef.current);
              } else {
                // Song 1: route directly to destination
                source.connect(analyserRef.current);
                source.connect(audioContext.destination);
              }

              audioSourcesRef.current[key] = source;
              connected = true;
            } catch (e) {
              // Source already connected or other error
            }
          }
        }
      }

      if (connected) {
        console.log('🎵 Audio sources connected (Song 2 routed through delay for beat offset)');
      }
    };

    connectSources();
  }, [stems]);

  // Update beat offset delay in real-time
  useEffect(() => {
    if (!audioContextRef.current || !delayNodeRef.current) return;

    // Get Song 2's BPM from metadata
    const song2BpmStr = metadata[1]?.bpm ? String(metadata[1].bpm) : '120';
    const song2Bpm = parseFloat(song2BpmStr.split('-')[0]) || 120;

    // Calculate delay in seconds: (beats / BPM) * 60
    const delaySeconds = beatOffset > 0 ? (beatOffset / song2Bpm) * 60 : 0;

    try {
      // Update delay time with smooth ramping
      delayNodeRef.current.delayTime.setValueAtTime(
        Math.max(0, Math.min(delaySeconds, 8)), // Clamp between 0-8 seconds
        audioContextRef.current.currentTime
      );

      if (beatOffset > 0) {
        console.log(`⏱️ Beat offset: ${beatOffset} beats @ ${song2Bpm} BPM = ${delaySeconds.toFixed(3)}s`);
      }
    } catch (e) {
      console.warn('Could not update delay time:', e);
    }
  }, [beatOffset, metadata]);

  // Load kick waveforms for visualization
  useEffect(() => {
    if (!metadata[0] && !metadata[1]) return;

    const loadKickWaveforms = async () => {
      try {
        if (!audioContextRef.current) {
          audioContextRef.current = new (window.AudioContext || window.webkitAudioContext)();
        }

        const context = audioContextRef.current;
        const kickData = { data1: null, data2: null, duration: 0 };

        // Load kick waveforms for both songs
        for (let slot = 0; slot < 2; slot++) {
          if (!metadata[slot]?.timestamp) continue;

          const kickUrl = `/api/audio/${metadata[slot].timestamp}/kick.wav`;

          try {
            const response = await fetch(kickUrl);
            const arrayBuffer = await response.arrayBuffer();
            const audioBuffer = await context.decodeAudioData(arrayBuffer);

            const rawData = audioBuffer.getChannelData(0);
            const samples = Math.min(rawData.length, 2048); // Limit samples for display
            const waveformData = new Float32Array(samples);

            for (let i = 0; i < samples; i++) {
              waveformData[i] = rawData[Math.floor((i / samples) * rawData.length)];
            }

            if (slot === 0) kickData.data1 = waveformData;
            if (slot === 1) kickData.data2 = waveformData;

            kickData.duration = Math.max(kickData.duration, audioBuffer.duration);
            console.log(`✅ Loaded kick waveform for Song ${slot + 1}`);
          } catch (e) {
            console.warn(`Could not load kick for Song ${slot + 1}:`, e.message);
          }
        }

        if (kickData.data1 && kickData.data2) {
          setKickWaveforms(kickData);
        }
      } catch (e) {
        console.warn('Waveform loading failed:', e);
      }
    };

    loadKickWaveforms();
  }, [metadata]);

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

  const handleBpmOverride = (slot, value) => {
    const newOverride = [...overrideBpm];
    newOverride[slot] = value ? parseFloat(value) : null;
    setOverrideBpm(newOverride);
  };

  const handleKeyOverride = (slot, value) => {
    const newOverride = [...overrideKey];
    newOverride[slot] = value || null;
    setOverrideKey(newOverride);
  };

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

    // Start polling for status updates
    const statusPoller = setInterval(async () => {
      try {
        const statusRes = await fetch('/api/process-status');
        const status = await statusRes.json();
        // DISABLED: Global /api/process-status causes race conditions with dual slots
        // if (status.slot === slot && status.status) { ... }
        // Just show processing state, rely on API response for final status
        if (status.slot !== undefined) {
          console.log(`⏳ API processing slot ${status.slot}: ${status.status}`);
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

        // Update audio refs to use processed stems
        Object.entries(data.processed_stems).forEach(([stemName, url]) => {
          if (audioRefsRef.current[slot][stemName]?.current) {
            audioRefsRef.current[slot][stemName].current.src = url;
            audioRefsRef.current[slot][stemName].current.load();
          }
        });

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

        // Update audio refs only after small delay to ensure files are written
        setTimeout(() => {
          Object.entries(data.processed_stems).forEach(([stemName, url]) => {
            if (audioRefsRef.current[slot][stemName]?.current) {
              audioRefsRef.current[slot][stemName].current.src = url;
              audioRefsRef.current[slot][stemName].current.load();
            }
          });
        }, 500);

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

    // Real progress tracking from API
    const progressInterval = setInterval(async () => {
      try {
        const res = await fetch('/api/process-status');
        const data = await res.json();
        if (data.progress) {
          setProcessingProgress(data.progress);
        }
      } catch (e) {
        // Silent - API might not have data yet
      }
    }, 500);

    try {
      // Process all loaded songs with both BPM and Key
      for (let slot = 0; slot < 2; slot++) {
        if (stems[slot]) {
          await processStems(slot, targetBpm, targetKey);
        }
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

  // Cleanup all audio files
  const handleCleanup = async () => {
    if (!confirm('🗑️ Delete all generated audio files? This cannot be undone.')) return;

    try {
      const response = await fetch('/api/cleanup', { method: 'POST' });
      const data = await response.json();

      if (response.ok) {
        alert('✅ Cleanup complete! All audio files deleted.');
        // Reset state
        setStems([null, null]);
        setMetadata([null, null]);
        setLoading([false, false]);
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

  // Render mixer for one song
  const renderSongMixer = (slot) => {
    const songName = metadata[slot]?.filename?.replace(/\.[^/.]+$/, '') || `Song ${slot + 1}`;

    return (
    <div className="song-mixer">
      <h3>{songName}</h3>

      {metadata[slot] && (
        <div className="metadata">
          <p><strong>{metadata[slot].filename}</strong></p>
          {stems[slot] && (
          <div style={{ marginTop: '10px', display: 'flex', gap: '20px', fontSize: '13px' }}>
            {/* BPM Override */}
            <div style={{ flex: 1 }}>
              <label style={{ color: '#999', fontSize: '11px' }}>BPM</label>
              {editingBpm[slot] ? (
                <div style={{ display: 'flex', gap: '5px', marginTop: '5px' }}>
                  <input
                    type="number"
                    value={overrideBpm[slot] !== null ? overrideBpm[slot] : metadata[slot].bpm}
                    onChange={(e) => handleBpmOverride(slot, e.target.value)}
                    style={{
                      flex: 1,
                      background: 'rgba(99, 102, 241, 0.2)',
                      border: '1px solid #6366f1',
                      color: '#fff',
                      padding: '4px 8px',
                      borderRadius: '4px',
                      fontSize: '12px'
                    }}
                  />
                  <button
                    onClick={() => setEditingBpm(prev => { const n = [...prev]; n[slot] = false; return n; })}
                    style={{
                      background: '#6366f1',
                      color: '#fff',
                      border: 'none',
                      padding: '4px 8px',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: '11px'
                    }}
                  >
                    ✓
                  </button>
                </div>
              ) : (
                <div style={{ display: 'flex', gap: '5px', marginTop: '5px', alignItems: 'center' }}>
                  <span style={{ color: overrideBpm[slot] !== null ? '#8b5cf6' : '#ccc' }}>
                    {getEffectiveBpm(slot)} {overrideBpm[slot] !== null ? '(custom)' : '(detected)'}
                  </span>
                  <button
                    onClick={() => setEditingBpm(prev => { const n = [...prev]; n[slot] = true; return n; })}
                    style={{
                      background: 'transparent',
                      color: '#6366f1',
                      border: '1px solid #6366f1',
                      padding: '2px 6px',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: '10px'
                    }}
                  >
                    ✎
                  </button>
                </div>
              )}
            </div>

            {/* Key Override */}
            <div style={{ flex: 1 }}>
              <label style={{ color: '#999', fontSize: '11px' }}>KEY</label>
              {editingKey[slot] ? (
                <div style={{ display: 'flex', gap: '5px', marginTop: '5px' }}>
                  <select
                    value={overrideKey[slot] !== null ? overrideKey[slot] : metadata[slot].key}
                    onChange={(e) => handleKeyOverride(slot, e.target.value)}
                    style={{
                      flex: 1,
                      background: 'rgba(99, 102, 241, 0.2)',
                      border: '1px solid #6366f1',
                      color: '#fff',
                      padding: '4px 8px',
                      borderRadius: '4px',
                      fontSize: '12px'
                    }}
                  >
                    <option value="">Clear override</option>
                    {KEYS.map(k => <option key={k} value={k}>{k}</option>)}
                  </select>
                  <button
                    onClick={() => setEditingKey(prev => { const n = [...prev]; n[slot] = false; return n; })}
                    style={{
                      background: '#6366f1',
                      color: '#fff',
                      border: 'none',
                      padding: '4px 8px',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: '11px'
                    }}
                  >
                    ✓
                  </button>
                </div>
              ) : (
                <div style={{ display: 'flex', gap: '5px', marginTop: '5px', alignItems: 'center' }}>
                  <span style={{ color: overrideKey[slot] !== null ? '#8b5cf6' : '#ccc' }}>
                    {getEffectiveKey(slot)} {overrideKey[slot] !== null ? '(custom)' : '(detected)'}
                  </span>
                  <button
                    onClick={() => setEditingKey(prev => { const n = [...prev]; n[slot] = true; return n; })}
                    style={{
                      background: 'transparent',
                      color: '#6366f1',
                      border: '1px solid #6366f1',
                      padding: '2px 6px',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: '10px'
                    }}
                  >
                    ✎
                  </button>
                </div>
              )}
            </div>
          </div>
          )}
        </div>
      )}

      {!stems[slot] ? (
        <>
          <StemLoader
            onStemsLoaded={(file) => handleStemsLoaded(file, slot)}
            loading={loading[slot]}
          />
        </>
      ) : (
        <div className="stem-controls">
          <h4>🎚️ Volumes</h4>
          {stemNames.map(stem => (
            <div key={stem} className="volume-control">
              <label>{stem.toUpperCase()}</label>
              <input
                type="range"
                min="0"
                max="1"
                step="0.01"
                value={volumes[slot]?.[stem] ?? 1.0}
                onChange={(e) => handleVolumeChange(slot, stem, parseFloat(e.target.value))}
                className="slider"
              />
              <span className="volume-value">
                {Math.round((volumes[slot]?.[stem] ?? 1.0) * 100)}%
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Hidden audio elements */}
      <div style={{ display: 'none' }}>
        {stemNames.map(stem => (
          <audio
            key={stem}
            ref={audioRefsRef.current[slot][stem]}
            crossOrigin="anonymous"
            onError={(e) => console.error(`Song ${slot + 1} ${stem} error:`, e)}
          />
        ))}
      </div>
    </div>
    );
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
          {renderSongMixer(0)}
          {renderSongMixer(1)}
        </div>

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
                disabled={isProcessing || isTransposing}
                style={{
                  opacity: (isProcessing || isTransposing) ? 0.5 : 1,
                  cursor: (isProcessing || isTransposing) ? 'not-allowed' : 'pointer'
                }}
              >
                {playing ? '⏸ PAUSE' : '▶ PLAY BOTH'}
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

            {/* Beat Offset for Song 2 */}
            <div className="crossfader-section" style={{ marginTop: '15px' }}>
              <label>🎵 Beat Offset (Song 2)</label>
              <div className="crossfader-labels">
                <span>Sync</span>
                <span>Offset</span>
                <span>+8 beats</span>
              </div>
              <input
                type="range"
                min="0"
                max="8"
                step="1"
                value={beatOffset}
                onChange={(e) => setBeatOffset(parseInt(e.target.value))}
                disabled={!stems[0] || !stems[1]}
                className="crossfader-slider"
                style={{
                  opacity: (!stems[0] || !stems[1]) ? 0.5 : 1,
                  cursor: (!stems[0] || !stems[1]) ? 'not-allowed' : 'pointer'
                }}
              />
              <div className="crossfader-value">
                {beatOffset === 0 ? 'No offset (sync)' : `+${beatOffset} beats`}
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
                    const statusEmoji = status === 'beatmatching' ? '⏳' : status === 'done' ? '✅' : '❌';
                    const sourceBpm = getEffectiveBpm(slot);
                    const sourceKey = getEffectiveKey(slot);
                    const targetBpmForSlot = targetBpm || (slot === 1 ? getEffectiveBpm(0) : null);
                    const targetKeyForSlot = targetKey || sourceKey;

                    return (
                      <div key={slot} style={{ color: '#aaa' }}>
                        <strong style={{ color: '#8b5cf6' }}>{metadata[slot]?.filename?.replace(/\.[^/.]+$/, '')}</strong>
                        <br />
                        {sourceBpm} → {targetBpmForSlot} BPM {sourceKey && targetKeyForSlot && `| ${sourceKey} → ${targetKeyForSlot}`} <span style={{ marginLeft: '8px' }}>{statusEmoji} {status === 'beatmatching' ? 'Beatmatching...' : status === 'done' ? 'Ready' : 'Failed'}</span>
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
                onClick={async () => {
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

                  const res = await fetch('/api/download-stems-zip', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                      timestamps,
                      metadata: metadataList,
                      include_original: true,
                      include_processed: false
                    })
                  });
                  const data = await res.json();
                  if (data.file) {
                    window.location.href = data.file;
                  }
                }}
                style={{
                  background: 'rgba(99, 102, 241, 0.2)',
                  color: '#a78bfa',
                  border: '1px solid #6366f1',
                  padding: '10px 16px',
                  borderRadius: '6px',
                  cursor: 'pointer',
                  fontSize: '12px',
                  fontWeight: 'bold'
                }}
              >
                📦 Original Stems
              </button>

              {/* Download Processed Stems */}
              {(transposedStems[0] || transposedStems[1] || beatmatchedStems[0] || beatmatchedStems[1]) && (
                <button
                  onClick={async () => {
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

                    const res = await fetch('/api/download-stems-zip', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({
                        timestamps,
                        metadata: metadataList,
                        include_original: false,
                        include_processed: true
                      })
                    });
                    const data = await res.json();
                    if (data.file) {
                      window.location.href = data.file;
                    }
                  }}
                  style={{
                    background: 'rgba(139, 92, 246, 0.2)',
                    color: '#c4b5fd',
                    border: '1px solid #8b5cf6',
                    padding: '10px 16px',
                    borderRadius: '6px',
                    cursor: 'pointer',
                    fontSize: '12px',
                    fontWeight: 'bold'
                  }}
                >
                  📦 Processed Stems
                </button>
              )}

              {/* Download Final Mix */}
              <button
                onClick={async () => {
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

                  const res = await fetch('/api/render-final-mix', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                      timestamps,
                      metadata: metadataList,
                      volumes,
                      crossfader,
                      beat_offsets: [0, beatOffset],
                      target_bpm: targetBpm
                    })
                  });
                  const data = await res.json();
                  if (data.file) {
                    window.location.href = data.file;
                  } else {
                    alert('Render failed: ' + data.error);
                  }
                }}
                style={{
                  background: 'rgba(34, 197, 94, 0.2)',
                  color: '#86efac',
                  border: '1px solid #22c55e',
                  padding: '10px 16px',
                  borderRadius: '6px',
                  cursor: 'pointer',
                  fontSize: '12px',
                  fontWeight: 'bold'
                }}
              >
                🎵 Final Mix (FLAC)
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
