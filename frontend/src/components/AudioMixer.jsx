import React, { useState, useRef, useEffect } from 'react';
import StemLoader from './StemLoader';
import MixerControls from './MixerControls';
import Waveform from './Waveform';
import '../styles/AudioMixer.css';

export default function AudioMixer() {
  const [stems, setStems] = useState({});
  const [metadata, setMetadata] = useState(null);
  const [loading, setLoading] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState('');

  // Web Audio API refs
  const audioContextRef = useRef(null);
  const audioBuffersRef = useRef({});
  const sourceNodesRef = useRef({});
  const gainNodesRef = useRef({});
  const masterGainRef = useRef(null);
  const analyserRef = useRef(null);
  const playbackStartTimeRef = useRef(0);
  const progressIntervalRef = useRef(null);

  // Control state
  const [volumes, setVolumes] = useState({
    vocals: 1.0,
    beats: 1.0,
    bass: 1.0,
    other: 1.0
  });

  const [pitch, setPitch] = useState(0);
  const [speed, setSpeed] = useState(1.0);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);

  // Initialize Web Audio API
  useEffect(() => {
    if (!audioContextRef.current) {
      try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        audioContextRef.current = ctx;

        // Create analyser for visualization
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 2048;
        analyserRef.current = analyser;

        // Create master gain node
        const masterGain = ctx.createGain();
        masterGain.gain.value = 0.8; // Prevent clipping

        // Connect: masterGain → analyser → destination
        masterGain.connect(analyser);
        analyser.connect(ctx.destination);
        masterGainRef.current = masterGain;

        console.log('✅ Web Audio API initialized');
        console.log('Context state:', ctx.state);
      } catch (err) {
        console.error('❌ Web Audio API error:', err);
        setError('Audio context failed: ' + err.message);
      }
    }
  }, []);

  // Handle stem upload and separation
  const handleStemsLoaded = async (file) => {
    setLoading(true);
    setError('');
    try {
      const formData = new FormData();
      formData.append('file', file);

      console.log('🔄 Uploading file for stem separation...');
      const response = await fetch('/api/separate-stems', {
        method: 'POST',
        body: formData
      });

      if (!response.ok) {
        throw new Error(`Server error: ${response.status}`);
      }

      const data = await response.json();
      console.log('✅ Stems received:', data);

      setStems(data.stems);
      setMetadata(data);

      // Pre-load all stems
      await loadStemsIntoAudio(data.stems);

      setError('');
      alert(`✅ Stems ready!\nBPM: ${data.bpm}\nKey: ${data.key}\n\nClick PLAY to start mixing!`);
    } catch (error) {
      console.error('❌ Error:', error);
      setError(`Error: ${error.message}`);
      alert(`❌ Error: ${error.message}`);
    } finally {
      setLoading(false);
    }
  };

  // Load audio files into Web Audio API
  const loadStemsIntoAudio = async (stemPaths) => {
    const ctx = audioContextRef.current;
    if (!ctx) {
      throw new Error('Audio context not initialized');
    }

    audioBuffersRef.current = {};

    for (const [stemName, stemUrl] of Object.entries(stemPaths)) {
      try {
        console.log(`Loading ${stemName} from ${stemUrl}`);
        const response = await fetch(stemUrl);
        if (!response.ok) throw new Error(`Failed to fetch ${stemName}`);

        const arrayBuffer = await response.arrayBuffer();
        const audioBuffer = await ctx.decodeAudioData(arrayBuffer);

        audioBuffersRef.current[stemName] = audioBuffer;
        setDuration(audioBuffer.duration);

        console.log(`✅ Loaded ${stemName}: ${audioBuffer.duration.toFixed(2)}s`);
      } catch (error) {
        console.error(`❌ Failed to load ${stemName}:`, error);
        setError(`Failed to load ${stemName}: ${error.message}`);
      }
    }
  };

  // Play/pause
  const togglePlayback = async () => {
    const ctx = audioContextRef.current;
    if (!ctx) {
      setError('Audio context not available');
      return;
    }

    try {
      if (playing) {
        // Stop playback
        Object.values(sourceNodesRef.current).forEach(source => {
          try {
            source.stop(0);
          } catch (e) {}
        });
        sourceNodesRef.current = {};
        setPlaying(false);
        clearInterval(progressIntervalRef.current);
        setCurrentTime(0);
      } else {
        // Resume context if suspended
        if (ctx.state === 'suspended') {
          console.log('Resuming audio context...');
          await ctx.resume();
        }

        // Clear old sources
        Object.values(sourceNodesRef.current).forEach(source => {
          try {
            source.stop(0);
          } catch (e) {}
        });
        sourceNodesRef.current = {};
        gainNodesRef.current = {};

        // Create new sources for all stems
        playbackStartTimeRef.current = ctx.currentTime;
        let minDuration = Infinity;

        for (const [stemName, audioBuffer] of Object.entries(audioBuffersRef.current)) {
          try {
            // Create source
            const source = ctx.createBufferSource();
            source.buffer = audioBuffer;
            source.playbackRate.value = speed || 1.0;

            // Create gain node
            const gainNode = ctx.createGain();
            gainNode.gain.value = volumes[stemName] || 1.0;

            // Connect: source → gain → master → destination
            source.connect(gainNode);
            gainNode.connect(masterGainRef.current);

            // Store references
            sourceNodesRef.current[stemName] = source;
            gainNodesRef.current[stemName] = gainNode;

            // Start playback
            source.start(0);
            minDuration = Math.min(minDuration, audioBuffer.duration);

            console.log(`▶ Playing ${stemName}`);
          } catch (err) {
            console.error(`Failed to play ${stemName}:`, err);
          }
        }

        setPlaying(true);

        // Update progress every 100ms
        clearInterval(progressIntervalRef.current);
        progressIntervalRef.current = setInterval(() => {
          const elapsed = ctx.currentTime - playbackStartTimeRef.current;
          setCurrentTime(Math.max(0, elapsed));

          if (elapsed >= minDuration) {
            // Playback finished
            Object.values(sourceNodesRef.current).forEach(source => {
              try {
                source.stop(0);
              } catch (e) {}
            });
            sourceNodesRef.current = {};
            setPlaying(false);
            clearInterval(progressIntervalRef.current);
            setCurrentTime(0);
          }
        }, 100);
      }
    } catch (err) {
      console.error('Playback error:', err);
      setError(`Playback error: ${err.message}`);
      setPlaying(false);
    }
  };

  // Update volume
  const handleVolumeChange = (stemName, value) => {
    const newVolumes = { ...volumes, [stemName]: value };
    setVolumes(newVolumes);

    // Apply to gain node if playing
    if (gainNodesRef.current[stemName]) {
      gainNodesRef.current[stemName].gain.value = value;
    }
  };

  // Format time
  const formatTime = (seconds) => {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  return (
    <div className="audio-mixer">
      <div className="mixer-section">
        <h2>🎵 Stem Mixer</h2>

        {/* File Upload */}
        <StemLoader onStemsLoaded={handleStemsLoaded} loading={loading} />

        {/* Error Display */}
        {error && (
          <div style={{
            background: 'rgba(239, 68, 68, 0.1)',
            border: '1px solid #ef4444',
            padding: '12px',
            borderRadius: '6px',
            color: '#fca5a5',
            marginBottom: '15px'
          }}>
            {error}
          </div>
        )}

        {/* Metadata Display */}
        {metadata && (
          <div className="metadata">
            <p><strong>File:</strong> {metadata.filename}</p>
            <p><strong>BPM:</strong> {metadata.bpm} | <strong>Key:</strong> {metadata.key}</p>
          </div>
        )}

        {/* Playback Controls */}
        {Object.keys(stems).length > 0 && (
          <>
            {/* Waveform Visualization */}
            <Waveform analyser={analyserRef.current} isPlaying={playing} />

            <div className="playback-controls">
              <button onClick={togglePlayback} className="play-btn">
                {playing ? '⏸ PAUSE' : '▶ PLAY'}
              </button>

              <div className="progress-bar">
                <div
                  className="progress-fill"
                  style={{ width: `${duration > 0 ? (currentTime / duration) * 100 : 0}%` }}
                ></div>
              </div>

              <span className="time-display">
                {formatTime(currentTime)} / {formatTime(duration)}
              </span>
            </div>
          </>
        )}

        {/* Mixer Controls */}
        {Object.keys(stems).length > 0 && (
          <MixerControls
            volumes={volumes}
            pitch={pitch}
            speed={speed}
            onVolumeChange={handleVolumeChange}
            onPitchChange={setPitch}
            onSpeedChange={setSpeed}
          />
        )}
      </div>
    </div>
  );
}
