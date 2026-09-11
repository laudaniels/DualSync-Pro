import React from 'react';
import StemLoader from './StemLoader';
import { stemNames, stemLabels } from './stemConstants';

const KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

// Split out of DualMixer so it can be memoized: the parent re-renders every
// 250ms while playing (to drive the progress bar/waveform), but nothing in
// here depends on currentTime -- without this split, that tick was forcing a
// full reconcile of volume sliders, upload UI, and 14 hidden <audio> elements
// per song, heavy enough to stall the main thread and starve all the audio
// elements' buffers at once.
function SongMixer({
  slot,
  metadata,
  hasStems,
  loading,
  editingBpm,
  editingKey,
  overrideBpm,
  overrideKey,
  effectiveBpm,
  effectiveKey,
  volumes,
  audioRefs,
  playingRef,
  logStemEvent,
  onBpmOverride,
  onKeyOverride,
  onToggleEditingBpm,
  onToggleEditingKey,
  onStemsLoaded,
  onVolumeChange
}) {
  const songName = metadata?.filename?.replace(/\.[^/.]+$/, '') || `Song ${slot + 1}`;

  return (
    <div className="song-mixer">
      <h3>{songName}</h3>

      {metadata && (
        <div className="metadata">
          <p><strong>{metadata.filename}</strong></p>
          {hasStems && (
          <div style={{ marginTop: '10px', display: 'flex', gap: '20px', fontSize: '13px' }}>
            {/* BPM Override */}
            <div style={{ flex: 1 }}>
              <label style={{ color: '#999', fontSize: '11px' }}>BPM</label>
              {editingBpm ? (
                <div style={{ display: 'flex', gap: '5px', marginTop: '5px' }}>
                  <input
                    type="number"
                    value={overrideBpm !== null ? overrideBpm : metadata.bpm}
                    onChange={(e) => onBpmOverride(slot, e.target.value)}
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
                    onClick={() => onToggleEditingBpm(slot, false)}
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
                  <span style={{ color: overrideBpm !== null ? '#8b5cf6' : '#ccc' }}>
                    {effectiveBpm} {overrideBpm !== null ? '(custom)' : '(detected)'}
                  </span>
                  <button
                    onClick={() => onToggleEditingBpm(slot, true)}
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
              {editingKey ? (
                <div style={{ display: 'flex', gap: '5px', marginTop: '5px' }}>
                  <select
                    value={overrideKey !== null ? overrideKey : metadata.key}
                    onChange={(e) => onKeyOverride(slot, e.target.value)}
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
                    onClick={() => onToggleEditingKey(slot, false)}
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
                  <span style={{ color: overrideKey !== null ? '#8b5cf6' : '#ccc' }}>
                    {effectiveKey} {overrideKey !== null ? '(custom)' : '(detected)'}
                  </span>
                  <button
                    onClick={() => onToggleEditingKey(slot, true)}
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

      {!hasStems ? (
        <StemLoader
          onStemsLoaded={(file) => onStemsLoaded(file, slot)}
          loading={loading}
        />
      ) : (
        <div className="stem-controls">
          <h4>🎚️ Volumes</h4>
          {stemNames.map(stem => (
            <div key={stem} className="volume-control" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <label style={{ minWidth: '80px' }}>{stemLabels[stem]}</label>
              <input
                type="range"
                min="0"
                max="1"
                step="0.01"
                value={volumes?.[stem] ?? 1.0}
                onChange={(e) => onVolumeChange(slot, stem, parseFloat(e.target.value))}
                className="slider"
                style={{ flex: 1 }}
              />
              <span className="volume-value" style={{ minWidth: '40px', textAlign: 'right' }}>
                {Math.round((volumes?.[stem] ?? 1.0) * 100)}%
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
            ref={audioRefs[stem]}
            crossOrigin="anonymous"
            loop
            onError={(e) => console.error(`Song ${slot + 1} ${stem} error:`, e)}
            onStalled={(e) => logStemEvent(slot, stem, 'stalled (browser can\'t fetch more data)', e.target)}
            onWaiting={(e) => logStemEvent(slot, stem, 'waiting (buffer underrun, audio goes silent here)', e.target)}
            onSuspend={(e) => logStemEvent(slot, stem, 'suspend (browser paused loading)', e.target)}
            onPause={(e) => {
              // A stem can stall out and pause itself (network hiccup, decode
              // stutter, buffer underrun) with no error event at all -- that's
              // the "some stems drop and only come back after pause/play"
              // symptom. If we still believe playback should be running,
              // resume this one stem immediately instead of making the user
              // manually pause/play everything to notice and fix it.
              if (playingRef.current && !e.target.ended) {
                logStemEvent(slot, stem, 'unexpected pause -- auto-resuming', e.target);
                e.target.play().catch(err => {
                  if (err.name !== 'AbortError') console.error(`Auto-resume failed (Song ${slot + 1} ${stem}):`, err);
                });
              }
            }}
          />
        ))}
      </div>
    </div>
  );
}

export default React.memo(SongMixer);
