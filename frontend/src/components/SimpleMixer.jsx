import React, { useState, useRef, useEffect } from 'react';
import StemLoader from './StemLoader';
import '../styles/AudioMixer.css';

export default function SimpleMixer() {
  const [stems, setStems] = useState({});
  const [metadata, setMetadata] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);

  // Audio refs
  const vocalsRef = useRef(null);
  const drumsRef = useRef(null);
  const bassRef = useRef(null);
  const otherRef = useRef(null);

  const audioRefs = {
    vocals: vocalsRef,
    drums: drumsRef,
    bass: bassRef,
    other: otherRef
  };

  const [volumes, setVolumes] = useState({
    vocals: 1.0,
    drums: 1.0,
    bass: 1.0,
    other: 1.0
  });

  // Handle stem upload
  const handleStemsLoaded = async (file) => {
    setLoading(true);
    setError('');
    try {
      const formData = new FormData();
      formData.append('file', file);

      console.log('🔄 Uploading for stem separation...');
      const response = await fetch('/api/separate-stems', {
        method: 'POST',
        body: formData
      });

      if (!response.ok) throw new Error(`Server error: ${response.status}`);

      const data = await response.json();
      console.log('✅ Stems received:', data);
      console.log('Stem URLs:', data.stems);

      setStems(data.stems);
      setMetadata(data);

      // Set audio sources directly
      Object.entries(data.stems).forEach(([stemName, url]) => {
        if (audioRefs[stemName]?.current) {
          console.log(`Setting ${stemName} src to: ${url}`);
          audioRefs[stemName].current.src = url;
          audioRefs[stemName].current.load();
        }
      });

      alert(`✅ Stems ready!\nBPM: ${data.bpm}\nKey: ${data.key}\n\nClick PLAY!`);
    } catch (error) {
      console.error('❌ Error:', error);
      setError(`Error: ${error.message}`);
    } finally {
      setLoading(false);
    }
  };

  // Play/pause
  const togglePlayback = () => {
    const allRefs = [vocalsRef, drumsRef, bassRef, otherRef];

    if (playing) {
      allRefs.forEach(ref => {
        if (ref.current) {
          ref.current.pause();
          ref.current.currentTime = 0;
        }
      });
      setPlaying(false);
      setCurrentTime(0);
    } else {
      allRefs.forEach(ref => {
        if (ref.current && ref.current.src) {
          ref.current.play().catch(e => console.error('Play error:', e));
        }
      });
      setPlaying(true);
    }
  };

  // Update volume
  const handleVolumeChange = (stem, value) => {
    setVolumes(prev => ({ ...prev, [stem]: value }));
    if (audioRefs[stem]?.current) {
      audioRefs[stem].current.volume = value;
    }
  };

  // Track progress
  useEffect(() => {
    if (!playing) return;

    const interval = setInterval(() => {
      if (vocalsRef.current) {
        setCurrentTime(vocalsRef.current.currentTime);
        setDuration(vocalsRef.current.duration || 0);

        if (vocalsRef.current.currentTime >= vocalsRef.current.duration - 0.1) {
          setPlaying(false);
          setCurrentTime(0);
        }
      }
    }, 100);

    return () => clearInterval(interval);
  }, [playing]);

  const formatTime = (seconds) => {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  return (
    <div className="audio-mixer">
      <div className="mixer-section">
        <h2>🎵 Stem Mixer</h2>

        <StemLoader onStemsLoaded={handleStemsLoaded} loading={loading} />

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

        {metadata && (
          <div className="metadata">
            <p><strong>File:</strong> {metadata.filename}</p>
            <p><strong>BPM:</strong> {metadata.bpm} | <strong>Key:</strong> {metadata.key}</p>
          </div>
        )}

        {Object.keys(stems).length > 0 && (
          <>
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

            <div className="mixer-controls">
              <div className="control-section">
                <h3>🎚️ Stem Volumes</h3>
                <div className="stem-controls">
                  {['vocals', 'drums', 'bass', 'other'].map(stem => (
                    <div key={stem} className="stem-control">
                      <label>{stem.toUpperCase()}</label>
                      <input
                        type="range"
                        min="0"
                        max="1"
                        step="0.01"
                        value={volumes[stem]}
                        onChange={(e) => handleVolumeChange(stem, parseFloat(e.target.value))}
                        className="fader"
                      />
                      <span className="volume-value">
                        {Math.round(volumes[stem] * 100)}%
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </>
        )}
      </div>

      {/* Audio elements - set src directly from state */}
      <audio
        ref={vocalsRef}
        crossOrigin="anonymous"
        onError={(e) => console.error('Vocals error:', e)}
      />
      <audio
        ref={drumsRef}
        crossOrigin="anonymous"
        onError={(e) => console.error('Drums error:', e)}
      />
      <audio
        ref={bassRef}
        crossOrigin="anonymous"
        onError={(e) => console.error('Bass error:', e)}
      />
      <audio
        ref={otherRef}
        crossOrigin="anonymous"
        onError={(e) => console.error('Other error:', e)}
      />
    </div>
  );
}
