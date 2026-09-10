export default function Mixer({ sliders, onSliderChange, settings, onSettingsChange }) {
  const handleSliderChange = (key, value) => {
    onSliderChange(prev => ({
      ...prev,
      [key]: parseFloat(value)
    }))
  }

  const handleSettingChange = (key, value) => {
    onSettingsChange(prev => ({
      ...prev,
      [key]: value
    }))
  }

  const stemNames = ['Vocals', 'Beats', 'Bass', 'Other']
  const stemKeys = ['vocals_vol', 'beats_vol', 'bass_vol', 'other_vol']

  return (
    <div className="mixer">
      {/* Song Controls */}
      {[0, 1].map(song => (
        <div key={song} style={{ marginBottom: '30px' }}>
          <h3 style={{ marginBottom: '15px', color: '#cbd5e1' }}>Song {song + 1}</h3>

          {/* Stem Sliders */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '15px', marginBottom: '20px' }}>
            {stemNames.map((stem, i) => {
              const key = `s${song}_${stemKeys[i]}`
              return (
                <div key={key}>
                  <label style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '5px', fontSize: '0.9em' }}>
                    <span>{stem}</span>
                    <span>{(sliders[key] * 100).toFixed(0)}%</span>
                  </label>
                  <input
                    type="range"
                    min="0"
                    max="2"
                    step="0.1"
                    value={sliders[key]}
                    onChange={(e) => handleSliderChange(key, e.target.value)}
                  />
                </div>
              )
            })}
          </div>

          {/* Other Controls */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '15px' }}>
            <div>
              <label style={{ display: 'block', marginBottom: '5px', fontSize: '0.9em' }}>
                Pitch Shift ({(sliders[`s${song}_pitch_shift`] || 0).toFixed(0)} semitones)
              </label>
              <input
                type="range"
                min="-12"
                max="12"
                step="1"
                value={sliders[`s${song}_pitch_shift`]}
                onChange={(e) => handleSliderChange(`s${song}_pitch_shift`, e.target.value)}
              />
            </div>
            <div>
              <label style={{ display: 'block', marginBottom: '5px', fontSize: '0.9em' }}>
                Speed ({(sliders[`s${song}_speed`] || 1).toFixed(2)}x)
              </label>
              <input
                type="range"
                min="0.5"
                max="2"
                step="0.1"
                value={sliders[`s${song}_speed`]}
                onChange={(e) => handleSliderChange(`s${song}_speed`, e.target.value)}
              />
            </div>
            <div>
              <label style={{ display: 'block', marginBottom: '5px', fontSize: '0.9em' }}>
                Reverb ({(sliders[`s${song}_reverb`] || 0).toFixed(2)})
              </label>
              <input
                type="range"
                min="0"
                max="1"
                step="0.1"
                value={sliders[`s${song}_reverb`]}
                onChange={(e) => handleSliderChange(`s${song}_reverb`, e.target.value)}
              />
            </div>
          </div>
        </div>
      ))}

      <div className="divider" />

      {/* Global Controls */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '20px' }}>
        <div>
          <label style={{ display: 'block', marginBottom: '5px', fontSize: '0.9em' }}>
            Crossfader: Song 1 ← {settings.crossfader}% → Song 2
          </label>
          <input
            type="range"
            min="0"
            max="100"
            step="1"
            value={settings.crossfader}
            onChange={(e) => handleSettingChange('crossfader', parseInt(e.target.value))}
          />
        </div>

        <div>
          <label style={{ display: 'block', marginBottom: '5px', fontSize: '0.9em' }}>
            Target BPM ({settings.target_bpm})
          </label>
          <input
            type="number"
            min="0"
            max="200"
            value={settings.target_bpm}
            onChange={(e) => handleSettingChange('target_bpm', parseInt(e.target.value))}
          />
        </div>
      </div>

      <div style={{ marginTop: '15px' }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer' }}>
          <input
            type="checkbox"
            checked={settings.beatmatch}
            onChange={(e) => handleSettingChange('beatmatch', e.target.checked)}
          />
          <span>Enable Beatmatch</span>
        </label>
      </div>
    </div>
  )
}
