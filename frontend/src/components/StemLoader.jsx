import React, { useRef } from 'react';

export default function StemLoader({ onStemsLoaded, loading }) {
  const fileInputRef = useRef(null);

  const handleFileSelect = (e) => {
    const file = e.target.files?.[0];
    if (file) {
      onStemsLoaded(file);
    }
  };

  const handleDrop = (e) => {
    e.preventDefault();
    e.stopPropagation();

    const file = e.dataTransfer.files?.[0];
    if (file && file.type.startsWith('audio/')) {
      onStemsLoaded(file);
    }
  };

  const handleDragOver = (e) => {
    e.preventDefault();
    e.stopPropagation();
  };

  return (
    <div
      className="stem-loader"
      onDrop={handleDrop}
      onDragOver={handleDragOver}
      onClick={() => fileInputRef.current?.click()}
      style={{
        border: '6px solid #7c3aed',
        borderRadius: '16px',
        padding: '50px 40px',
        textAlign: 'center',
        cursor: 'pointer',
        background: 'rgba(124, 58, 237, 0.45)',
        width: '100%',
        minHeight: '350px',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        boxSizing: 'border-box',
        boxShadow: `
          inset 0 0 50px rgba(124, 58, 237, 0.6),
          0 0 40px rgba(124, 58, 237, 0.5)
        `,
        transition: 'all 0.2s ease'
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = '#a78bfa';
        e.currentTarget.style.background = 'rgba(124, 58, 237, 0.6)';
        e.currentTarget.style.boxShadow = `
          inset 0 0 60px rgba(167, 139, 250, 0.7),
          0 0 50px rgba(167, 139, 250, 0.6)
        `;
        e.currentTarget.style.transform = 'scale(1.02)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = '#7c3aed';
        e.currentTarget.style.background = 'rgba(124, 58, 237, 0.45)';
        e.currentTarget.style.boxShadow = `
          inset 0 0 50px rgba(124, 58, 237, 0.6),
          0 0 40px rgba(124, 58, 237, 0.5)
        `;
        e.currentTarget.style.transform = 'scale(1)';
      }}
    >
      {loading ? (
        <div className="loader">
          <div className="spinner"></div>
          <p>Separating stems... (this may take a minute)</p>
        </div>
      ) : (
        <>
          <input
            ref={fileInputRef}
            type="file"
            accept="audio/*"
            onChange={handleFileSelect}
            style={{ display: 'none' }}
          />
          <div className="upload-prompt">
            <p style={{ fontSize: '32px' }}>🎵</p>
            <p><strong>Drop audio file here or click to upload</strong></p>
            <p style={{ fontSize: '12px', color: '#888' }}>
              Supports MP3, WAV, M4A, FLAC, and more
            </p>
          </div>
        </>
      )}
    </div>
  );
}
