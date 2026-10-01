import React from 'react';
import StemLoader from './StemLoader';
import WaveformPreview from './WaveformPreview';
import { stemLabels } from './stemConstants';
import { formatKey } from './camelotWheel';

const KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

// Split out of DualMixer so it can be memoized: the parent re-renders every
// 100ms while playing (to drive the progress bar/waveform), but nothing in
// here depends on currentTime -- without this split, that tick was forcing a
// full reconcile of the volume sliders/upload UI every tick for no reason.
function SongMixer({
  slot,
  metadata,
  hasStems,
  revealVolumes,
  bothAnalyzed,
  loadProgress,
  loading,
  editingBpm,
  editingKey,
  overrideBpm,
  overrideKey,
  effectiveBpm,
  effectiveKey,
  scale,
  volumes,
  restoredStems,
  restoringStem,
  playing,
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
  onVolumeChange,
  onSetAllVolumes,
  onToggleRestoration
}) {
  const songName = metadata?.filename?.replace(/\.[^/.]+$/, '') || `Song ${slot + 1}`;
  // BPM/Key already known from the fast analyze step, whether or not stems
  // exist yet -- drives showing the detected-info block and (pre-stems)
  // which "not ready yet" sub-state to render below.
  const analyzed = metadata?.bpm != null;
  // Once both songs are analyzed and the Processing section takes over
  // (see the `bothAnalyzed` box below), this card no longer has an upload
  // box, spinner, or sliders to show -- just the header + detected info --
  // so its normal fixed min-height would leave a lot of dead space.
  const collapsed = !hasStems && analyzed && bothAnalyzed;

  return (
    <div className="song-mixer" style={collapsed ? { minHeight: 0 } : undefined}>
      <h3>{songName}</h3>

      {metadata && (
        <div className="metadata">
          <p><strong>{metadata.filename}</strong></p>
          {(metadata.detectedBpm || metadata.detectedKey) && (
            <p style={{ margin: '2px 0 0 0', fontSize: '11px', color: '#888' }}>
              {metadata.mode === 'snap' ? '🧲 Original' : 'Detected'}: {metadata.detectedBpm} BPM{metadata.detectedBpm && metadata.detectedKey ? ', ' : ''}{formatKey(metadata.detectedKey, scale)}
            </p>
          )}
          {metadata.bpm && metadata.mode === 'snap' && (
            <p style={{ margin: '2px 0 0 0', fontSize: '11px', color: '#888' }}>
              🧲 Snapped to: {metadata.bpm} BPM
            </p>
          )}
          {metadata.bpm && metadata.mode === 'align' && metadata.detectedBpm && metadata.bpm !== metadata.detectedBpm && (
            <p style={{ margin: '2px 0 0 0', fontSize: '11px', color: '#888' }}>
              🎯 Aligned to: {metadata.bpm} BPM
            </p>
          )}
          {metadata.gridCorrection && (
            <p style={{ margin: '2px 0 0 0', fontSize: '11px', color: '#888' }}>
              {metadata.mode === 'snap' ? '🧲 Snap' : '🎯 Alignment'} corrected: {metadata.gridCorrection.mean_ms}ms avg (max {metadata.gridCorrection.max_ms}ms)
            </p>
          )}
          {metadata.bpm != null && (
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
                      background: '#1a1f3a',
                      border: '1px solid #6366f1',
                      color: '#fff',
                      padding: '4px 8px',
                      borderRadius: '4px',
                      fontSize: '12px',
                      colorScheme: 'dark'
                    }}
                  >
                    <option value="" style={{ background: '#1a1f3a', color: '#fff' }}>Clear override</option>
                    {KEYS.map(k => <option key={k} value={k} style={{ background: '#1a1f3a', color: '#fff' }}>{formatKey(k, scale)}</option>)}
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
                    {formatKey(effectiveKey, scale)} {overrideKey !== null ? '(custom)' : '(detected)'}
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
        analyzed ? (
          // Once both songs are analyzed, the Processing section below is
          // enabled and becomes the single source of truth (choice, then
          // combined progress bar) -- this box (and its "waiting"/
          // "separating" status, whose text can run long and wrap,
          // jittering the layout) would just be redundant with it, so it's
          // hidden entirely rather than kept in sync with it.
          bothAnalyzed ? null : (
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
              <p style={{ color: '#ccc', fontSize: '13px' }}>
                ✅ Analyzed — waiting for the other song to finish analyzing...
              </p>
            </div>
          )
        ) : pendingSong ? (
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
                  {loadProgress?.current_step || (
                    processingStage === 'align' ? 'Aligning beatgrid...'
                    : processingStage === 'snap' ? 'Snapping beat grid to Song 1...'
                    : 'Analyzing BPM and Key...'
                  )}
                </p>
                {loadProgress?.progress != null && (
                  <div style={{ width: '80%' }}>
                    <div style={{
                      background: 'rgba(99, 102, 241, 0.15)',
                      border: '1px solid #6366f1',
                      borderRadius: '4px',
                      overflow: 'hidden',
                      height: '20px'
                    }}>
                      <div style={{
                        width: `${loadProgress.progress}%`,
                        height: '100%',
                        background: 'linear-gradient(90deg, #6366f1, #8b5cf6)',
                        transition: 'width 0.3s ease',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        color: '#fff',
                        fontSize: '11px',
                        fontWeight: 'bold'
                      }}>
                        {Math.round(loadProgress.progress)}%
                      </div>
                    </div>
                  </div>
                )}
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
                      2️⃣ Align beatgrid
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
            progress={loadProgress?.progress}
            stepLabel={loadProgress?.current_step}
          />
        )
      ) : !revealVolumes ? (
        <div className="stem-controls" style={{ textAlign: 'center', padding: '30px 0', color: '#999', fontSize: '13px' }}>
          ✅ Stems ready — waiting for the other song to finish...
        </div>
      ) : (
        <div className="stem-controls">
          <h4 style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '8px' }}>
            <span>🎚️ Volumes {!audioReady && <span style={{ fontWeight: 'normal', fontSize: '11px', color: '#999' }}>(⏳ preparing audio...)</span>}</span>
            <span style={{ display: 'flex', gap: '6px' }}>
              <button
                type="button"
                onClick={() => onSetAllVolumes(slot, 0)}
                title="Set every stem for this song to 0%"
                style={{
                  fontSize: '11px',
                  fontWeight: 'normal',
                  padding: '3px 8px',
                  borderRadius: '5px',
                  border: '1px solid rgba(255, 255, 255, 0.2)',
                  background: 'transparent',
                  color: '#ccc',
                  cursor: 'pointer'
                }}
              >
                🔇 All to 0%
              </button>
              <button
                type="button"
                onClick={() => onSetAllVolumes(slot, 1)}
                title="Set every stem for this song to 100%"
                style={{
                  fontSize: '11px',
                  fontWeight: 'normal',
                  padding: '3px 8px',
                  borderRadius: '5px',
                  border: '1px solid rgba(255, 255, 255, 0.2)',
                  background: 'transparent',
                  color: '#ccc',
                  cursor: 'pointer'
                }}
              >
                🔊 All to 100%
              </button>
            </span>
          </h4>
          {(stemNames || []).map((stem, i) => (
            <div
              key={stem}
              className="volume-control"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                paddingBottom: '10px',
                marginBottom: '10px',
                borderBottom: i < stemNames.length - 1 ? '1px solid rgba(255, 255, 255, 0.12)' : 'none'
              }}
            >
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
              {/* Denoise + de-reverb only makes sense for vocals -- these
                  models are trained for vocal cleanup, and testing showed
                  they gut non-vocal stems instead of helping (e.g. guitar
                  measured ~11dB quieter after "restoration"). No checkbox
                  for stems it can only hurt. */}
              {stem === 'vocals' && (
                <label
                  title="Denoise + de-reverb restoration (can only be toggled while nothing is playing)"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '3px',
                    fontSize: '11px',
                    color: '#999',
                    cursor: (playing || restoringStem?.[`${slot}:${stem}`]) ? 'not-allowed' : 'pointer',
                    opacity: (playing || restoringStem?.[`${slot}:${stem}`]) ? 0.5 : 1
                  }}
                >
                  <input
                    type="checkbox"
                    checked={!!restoredStems?.[stem]}
                    disabled={playing || !!restoringStem?.[`${slot}:${stem}`]}
                    onChange={(e) => onToggleRestoration(slot, stem, e.target.checked)}
                  />
                  {restoringStem?.[`${slot}:${stem}`] ? '⏳' : '✨'}
                </label>
              )}
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
