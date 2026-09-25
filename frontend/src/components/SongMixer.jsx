import React from 'react';
import StemLoader from './StemLoader';
import WaveformPreview from './WaveformPreview';
import { stemLabels } from './stemConstants';

const KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

// Split out of DualMixer so it can be memoized: the parent re-renders every
// 100ms while playing (to drive the progress bar/waveform), but nothing in
// here depends on currentTime -- without this split, that tick was forcing a
// full reconcile of the volume sliders/upload UI every tick for no reason.
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
  stemNames,
  audioReady,
  pendingSong,
  processingStage,
  canSnapToSong1,
  waitingForSong1,
  onBpmOverride,
  onKeyOverride,
  onToggleEditingBpm,
  onToggleEditingKey,
  onFileDropped,
  onChooseMode,
  onVolumeChange
}) {
  const songName = metadata?.filename?.replace(/\.[^/.]+$/, '') || `Song ${slot + 1}`;

  return (
    <div className="song-mixer">
      <h3>{songName}</h3>

      {metadata && (
        <div className="metadata">
          <p><strong>{metadata.filename}</strong></p>
          {hasStems && (metadata.detectedBpm || metadata.detectedKey) && (
            <p style={{ margin: '2px 0 0 0', fontSize: '11px', color: '#888' }}>
              {metadata.mode === 'snap' ? '🧲 Original' : 'Detected'}: {metadata.detectedBpm} BPM{metadata.detectedBpm && metadata.detectedKey ? ', ' : ''}{metadata.detectedKey}
            </p>
          )}
          {hasStems && metadata.bpm && metadata.mode === 'snap' && (
            <p style={{ margin: '2px 0 0 0', fontSize: '11px', color: '#888' }}>
              🧲 Snapped to: {metadata.bpm} BPM
            </p>
          )}
          {hasStems && metadata.bpm && metadata.mode === 'align' && metadata.detectedBpm && metadata.bpm !== metadata.detectedBpm && (
            <p style={{ margin: '2px 0 0 0', fontSize: '11px', color: '#888' }}>
              🎯 Aligned to: {metadata.bpm} BPM
            </p>
          )}
          {hasStems && metadata.gridCorrection && (
            <p style={{ margin: '2px 0 0 0', fontSize: '11px', color: '#888' }}>
              {metadata.mode === 'snap' ? '🧲 Snap' : '🎯 Alignment'} corrected: {metadata.gridCorrection.mean_ms}ms avg (max {metadata.gridCorrection.max_ms}ms)
            </p>
          )}
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
        pendingSong ? (
          <div className="stem-loader" style={{
            border: '6px solid #7c3aed',
            borderRadius: '16px',
            padding: '50px 40px',
            textAlign: 'center',
            background: 'rgba(124, 58, 237, 0.45)',
            width: '100%',
            minHeight: '350px',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            boxSizing: 'border-box',
            gap: '16px'
          }}>
            {loading ? (
              <div className="loader">
                <div className="spinner"></div>
                <p>
                  {processingStage === 'align' ? 'Aligning beatgrid...'
                    : processingStage === 'snap' ? 'Snapping beat grid to Song 1...'
                    : 'Separating stems... (this may take a minute)'}
                </p>
              </div>
            ) : waitingForSong1 ? (
              <div className="loader">
                <div className="spinner"></div>
                <p>⏳ Waiting for Song 1 to finish, so all 3 options (including snapping to it) can be shown together...</p>
              </div>
            ) : (
              <>
                <p><strong>How should this song be processed?</strong></p>
                <div style={{ width: '100%', display: 'flex', flexDirection: 'column', gap: '14px', alignItems: 'center' }}>
                  <div style={{ width: '80%' }}>
                    <button
                      onClick={() => onChooseMode(slot, 'as_is')}
                      style={{
                        background: '#6366f1',
                        color: '#fff',
                        border: 'none',
                        padding: '14px 24px',
                        borderRadius: '8px',
                        cursor: 'pointer',
                        fontSize: '14px',
                        width: '100%'
                      }}
                    >
                      1️⃣ Process as is
                    </button>
                    <p style={{ margin: '4px 0 0 0', fontSize: '11px', color: '#999' }}>Keep original timing, no correction</p>
                  </div>

                  <div style={{ width: '80%' }}>
                    <button
                      onClick={() => onChooseMode(slot, 'align')}
                      style={{
                        background: 'transparent',
                        color: '#6366f1',
                        border: '2px solid #6366f1',
                        padding: '14px 24px',
                        borderRadius: '8px',
                        cursor: 'pointer',
                        fontSize: '14px',
                        width: '100%'
                      }}
                    >
                      2️⃣ Align beatgrid first
                    </button>
                    <p style={{ margin: '4px 0 0 0', fontSize: '11px', color: '#999' }}>Correct internal timing drift (vinyl rip, live recording)</p>
                  </div>

                  {canSnapToSong1 && (
                    <div style={{ width: '80%' }}>
                      <button
                        onClick={() => onChooseMode(slot, 'snap')}
                        style={{
                          background: 'transparent',
                          color: '#22c55e',
                          border: '2px solid #22c55e',
                          padding: '14px 24px',
                          borderRadius: '8px',
                          cursor: 'pointer',
                          fontSize: '14px',
                          width: '100%'
                        }}
                      >
                        3️⃣ 🧲 Snap beat grid to Song 1
                      </button>
                      <p style={{ margin: '4px 0 0 0', fontSize: '11px', color: '#999' }}>Lock to Song 1's beat grid (fastest sync)</p>
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        ) : (
          <StemLoader
            onStemsLoaded={(file) => onFileDropped(file, slot)}
            loading={loading}
            loadingLabel="Converting to WAV..."
          />
        )
      ) : (
        <div className="stem-controls">
          <h4>🎚️ Volumes {!audioReady && <span style={{ fontWeight: 'normal', fontSize: '11px', color: '#999' }}>(⏳ preparing audio...)</span>}</h4>
          {(stemNames || []).map(stem => (
            <div key={stem} className="volume-control" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <label style={{ minWidth: '80px' }}>{stemLabels[stem] || stem}</label>
              <div style={{ width: '120px', height: '40px' }}>
                {metadata?.stems?.[stem] && (
                  <WaveformPreview
                    stemPath={metadata.stems[stem]}
                    stemName={stem}
                    width={120}
                    height={40}
                  />
                )}
              </div>
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
    </div>
  );
}

export default React.memo(SongMixer);
