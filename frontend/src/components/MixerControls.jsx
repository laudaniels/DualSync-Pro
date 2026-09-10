import React from 'react';

export default function MixerControls({
  volumes,
  pitch,
  speed,
  onVolumeChange,
  onPitchChange,
  onSpeedChange
}) {
  const stems = ['vocals', 'beats', 'bass', 'other'];

  return (
    <div className="mixer-controls">
      <div className="control-section">
        <h3>🎚️ Stem Volumes</h3>
        <div className="stem-controls">
          {stems.map(stem => (
            <div key={stem} className="stem-control">
              <label>{stem.toUpperCase()}</label>
              <input
                type="range"
                min="0"
                max="1"
                step="0.01"
                value={volumes[stem] || 0}
                onChange={(e) => onVolumeChange(stem, parseFloat(e.target.value))}
                className="fader"
              />
              <span className="volume-value">
                {Math.round(volumes[stem] * 100)}%
              </span>
            </div>
          ))}
        </div>
      </div>

      <div className="control-section">
        <h3>🎛️ Global Effects</h3>

        <div className="effect-control">
          <label>Pitch Shift (semitones)</label>
          <input
            type="range"
            min="-12"
            max="12"
            step="1"
            value={pitch}
            onChange={(e) => onPitchChange(parseFloat(e.target.value))}
            className="slider"
          />
          <span className="effect-value">{pitch > 0 ? '+' : ''}{pitch}</span>
        </div>

        <div className="effect-control">
          <label>Speed</label>
          <input
            type="range"
            min="0.5"
            max="2"
            step="0.1"
            value={speed}
            onChange={(e) => onSpeedChange(parseFloat(e.target.value))}
            className="slider"
          />
          <span className="effect-value">{speed.toFixed(1)}x</span>
        </div>
      </div>

      <div className="control-section">
        <h3>💡 Tips</h3>
        <ul>
          <li>Adjust stem volumes to create custom mixes</li>
          <li>Use pitch shift to match keys between songs</li>
          <li>Speed adjusts playback tempo without changing pitch</li>
        </ul>
      </div>
    </div>
  );
}
