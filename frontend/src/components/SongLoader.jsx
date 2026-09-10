import { useState } from 'react'

export default function SongLoader({ onSongsLoaded, onStemsReady, onStatus }) {
  const [loading, setLoading] = useState([false, false])

  const handleFileUpload = async (slot, file) => {
    if (!file) return

    const newLoading = [...loading]
    newLoading[slot] = true
    setLoading(newLoading)

    const formData = new FormData()
    formData.append('file', file)

    try {
      const response = await fetch(`/api/load-song/${slot}`, {
        method: 'POST',
        body: formData
      })

      if (!response.ok) throw new Error('Upload failed')

      const data = await response.json()
      onStatus(`✓ Song ${slot + 1} loaded: ${file.name}`)
      onSongsLoaded(prev => {
        const songs = [...prev]
        songs[slot] = data.path
        return songs
      })

      // Note: App.jsx polling will detect when stems are ready
      // No need to check here

    } catch (error) {
      onStatus(`✗ Error loading song: ${error.message}`)
    } finally {
      newLoading[slot] = false
      setLoading(newLoading)
    }
  }

  return (
    <div className="song-loader">
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '20px' }}>
        {[0, 1].map(slot => (
          <div key={slot} style={{
            padding: '20px',
            background: '#0f172a',
            borderRadius: '6px',
            border: '1px dashed #475569'
          }}>
            <h3 style={{ marginBottom: '10px' }}>Song {slot + 1}</h3>
            <input
              type="file"
              accept="audio/*"
              onChange={(e) => handleFileUpload(slot, e.target.files[0])}
              disabled={loading[slot]}
              style={{ width: '100%', padding: '10px' }}
            />
            {loading[slot] && <div className="spinner" style={{ marginTop: '10px' }} />}
          </div>
        ))}
      </div>
    </div>
  )
}
