import { useState, useRef, useEffect } from 'react'

export default function LoopPlayer({ sliders, settings, loopFile, onLoopReady, onStatus, onSettingsChange }) {
  const [rendering, setRendering] = useState(false)
  const [loopActive, setLoopActive] = useState(false)
  const audioRef = useRef(null)
  const fileCheckInterval = useRef(null)
  const [lastFileSize, setLastFileSize] = useState(0)

  const handleSettingChange = (key, value) => {
    if (onSettingsChange) {
      onSettingsChange(prev => ({ ...prev, [key]: value }))
    }
  }

  useEffect(() => {
    // Check for file updates every 1 second
    fileCheckInterval.current = setInterval(async () => {
      if (!loopFile) return

      try {
        const response = await fetch(loopFile, { method: 'HEAD' })
        const fileSize = parseInt(response.headers.get('content-length') || 0)

        if (lastFileSize > 0 && fileSize !== lastFileSize) {
          console.log('[Loop] File changed:', lastFileSize, '→', fileSize)
          setLastFileSize(fileSize)

          // Reload audio using loopFile state (don't rely on audioRef.current.src which might be empty)
          if (audioRef.current && loopFile) {
            const wasPlaying = !audioRef.current.paused
            audioRef.current.pause()
            const newSrc = loopFile + '?t=' + Date.now()
            audioRef.current.src = newSrc
            audioRef.current.load()

            if (wasPlaying) {
              setTimeout(() => {
                audioRef.current?.play().catch(e => console.log('Play failed:', e))
              }, 50)
            }
          }
        } else if (!lastFileSize && fileSize) {
          setLastFileSize(fileSize)
        }
      } catch (e) {
        console.log('File check failed:', e)
      }
    }, 1000)

    return () => clearInterval(fileCheckInterval.current)
  }, [loopFile, lastFileSize])

  const handleStartLoop = async () => {
    setRendering(true)
    onStatus('🎵 Rendering loop...')

    try {
      const response = await fetch('/api/start-loop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          loop_start: settings.loop_start,
          loop_length: settings.loop_length,
          sliders,
          crossfader: settings.crossfader,
          target_bpm: settings.target_bpm,
          beatmatch: settings.beatmatch,
        })
      })

      if (!response.ok) throw new Error('Loop render failed')

      const data = await response.json()
      onLoopReady(data.file)
      setLoopActive(true)
      setLastFileSize(0)
      onStatus('✨ Loop ready! Playing...')

      // Auto-play
      if (audioRef.current) {
        audioRef.current.src = data.file
        audioRef.current.load()
        audioRef.current.play().catch(e => console.log('Autoplay prevented:', e))
      }

    } catch (error) {
      onStatus(`✗ Error: ${error.message}`)
    } finally {
      setRendering(false)
    }
  }

  return (
    <div className="loop-player">
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px', marginBottom: '20px' }}>
        <div>
          <label style={{ display: 'block', marginBottom: '5px', fontSize: '0.9em' }}>
            Loop Start (seconds)
          </label>
          <input
            type="number"
            min="0"
            max="120"
            value={settings.loop_start}
            onChange={(e) => handleSettingChange('loop_start', parseInt(e.target.value) || 0)}
          />
        </div>

        <div>
          <label style={{ display: 'block', marginBottom: '5px', fontSize: '0.9em' }}>
            Loop Length
          </label>
          <select
            value={settings.loop_length}
            onChange={(e) => handleSettingChange('loop_length', e.target.value)}
          >
            <option value="4 bars (10s)">4 bars (10s)</option>
            <option value="8 bars (20s)">8 bars (20s)</option>
            <option value="16 bars (40s)">16 bars (40s)</option>
          </select>
        </div>
      </div>

      <button
        onClick={handleStartLoop}
        disabled={rendering}
        style={{
          width: '100%',
          padding: '15px',
          fontSize: '1.1em',
          marginBottom: '20px',
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          gap: '10px'
        }}
      >
        {rendering && <div className="spinner" style={{ width: '14px', height: '14px' }} />}
        {rendering ? 'Rendering...' : '▶ START LOOP'}
      </button>

      {loopActive && (
        <div style={{
          background: '#0f172a',
          padding: '15px',
          borderRadius: '6px',
          border: '1px solid #334155'
        }}>
          <label style={{ display: 'block', marginBottom: '10px', fontSize: '0.9em', color: '#a78bfa' }}>
            Loop Output
          </label>
          <audio
            ref={audioRef}
            controls
            loop
            style={{
              width: '100%',
              marginTop: '10px'
            }}
            preload="auto"
          />
          {loopFile && (
            <div style={{ marginTop: '10px', fontSize: '0.8em', color: '#94a3b8' }}>
              Playing: {loopFile.split('/').pop()}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
