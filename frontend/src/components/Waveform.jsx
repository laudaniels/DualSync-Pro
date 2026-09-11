import React, { useEffect, useRef, useState } from 'react';

export default function Waveform({ kicks, currentTime, beatOffset, song2Bpm, zoomLevel = 10, onZoomChange }) {
  const canvasRef = useRef(null);
  const [loading, setLoading] = useState(true);
  const ZOOM_WINDOW = zoomLevel; // Dynamic zoom level

  // Colors for each stem
  const stemColors = {
    vocals: 'rgb(168, 85, 247)',    // purple
    kick: 'rgb(99, 102, 241)',      // indigo
    snare: 'rgb(236, 72, 153)',     // pink
    hihat: 'rgb(249, 115, 22)',     // orange
    tom: 'rgb(34, 197, 94)',        // green
    bass: 'rgb(59, 130, 246)',      // blue
    other: 'rgb(168, 162, 158)'     // gray
  };

  const stemLabels = {
    vocals: '🎤 Vocals',
    kick: '🔊 Kick',
    snare: '🥁 Snare',
    hihat: '⚡ Hi-Hat',
    tom: '🔔 Tom',
    bass: '🎸 Bass',
    other: '🎹 Other'
  };

  useEffect(() => {
    if (!canvasRef.current) return;

    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    const width = canvas.offsetWidth;
    const height = canvas.offsetHeight;

    canvas.width = width * window.devicePixelRatio;
    canvas.height = height * window.devicePixelRatio;
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);

    // Clear canvas
    ctx.fillStyle = 'rgba(15, 15, 30, 0.95)';
    ctx.fillRect(0, 0, width, height);

    if (!kicks || !kicks.stems || !Object.keys(kicks.stems).length) {
      ctx.fillStyle = '#666';
      ctx.font = '12px monospace';
      ctx.textAlign = 'center';
      ctx.fillText('Select stems to display', width / 2, height / 2);
      return;
    }

    setLoading(false);

    const stemsToDisplay = Object.keys(kicks.stems);
    const stemsPerSlot = 2; // Song 1 and Song 2 for each stem
    const rowHeight = height / stemsToDisplay.length;

    // Calculate zoom window around currentTime
    const windowStart = Math.max(0, currentTime - ZOOM_WINDOW / 2);
    const windowEnd = windowStart + ZOOM_WINDOW;
    const totalDuration = kicks.duration || 10;

    const actualStart = Math.min(windowStart, totalDuration - ZOOM_WINDOW);
    const actualEnd = actualStart + ZOOM_WINDOW;

    // Calculate beat offset delay in seconds
    const delaySeconds = beatOffset > 0 ? (beatOffset / song2Bpm) * 60 : 0;

    // Calculate pixel offset for Song 2 based on beat offset
    const song2PixelOffset = (delaySeconds / ZOOM_WINDOW) * width;

    // Draw each selected stem
    stemsToDisplay.forEach((stem, stemIdx) => {
      const yOffset = (stemIdx + 0.5) * rowHeight;
      const stemData = kicks.stems[stem];
      const color = stemColors[stem];

      // Draw both Song 1 and Song 2 for this stem
      [
        { data: stemData.data1, offset: 0, label: `${stemLabels[stem]} (Song 1)`, xShift: 0 },
        { data: stemData.data2, offset: rowHeight / 2, label: `Song 2`, xShift: song2PixelOffset }
      ].forEach((songData, songIdx) => {
        if (!songData.data) return;

        const yPos = yOffset + (songIdx - 0.5) * (rowHeight / 3);
        const totalSamples = songData.data.length;
        const startSample = Math.floor((actualStart / totalDuration) * totalSamples);
        const endSample = Math.floor((actualEnd / totalDuration) * totalSamples);
        const visibleSamples = endSample - startSample;

        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        ctx.globalAlpha = songIdx === 0 ? 1 : 0.6; // Song 2 slightly faded
        ctx.beginPath();

        const pixelsPerSample = width / visibleSamples;
        for (let i = 0; i < visibleSamples; i++) {
          const sampleIdx = startSample + i;
          if (sampleIdx >= songData.data.length) break;

          const x = songData.xShift + (i * pixelsPerSample);

          // Skip drawing if x is outside canvas for Song 2 shift
          if (x < 0 || x > width) continue;

          const amplitude = songData.data[sampleIdx] || 0;
          const y = yPos + (amplitude * (rowHeight / 4));

          if (i === 0) {
            ctx.moveTo(x, y);
          } else {
            ctx.lineTo(x, y);
          }
        }
        ctx.stroke();
        ctx.globalAlpha = 1;

        // Draw center line
        ctx.strokeStyle = 'rgba(99, 102, 241, 0.1)';
        ctx.beginPath();
        ctx.moveTo(0, yPos);
        ctx.lineTo(width, yPos);
        ctx.stroke();
      });

      // Draw stem label
      ctx.fillStyle = color;
      ctx.font = 'bold 10px monospace';
      ctx.textAlign = 'left';
      ctx.fillText(stemLabels[stem], 5, yOffset - rowHeight / 4);
    });

    // Draw playhead
    const playheadPercent = (currentTime - actualStart) / ZOOM_WINDOW;
    if (playheadPercent >= 0 && playheadPercent <= 1) {
      const playheadX = playheadPercent * width;
      ctx.strokeStyle = 'rgba(255, 87, 87, 0.8)';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(playheadX, 0);
      ctx.lineTo(playheadX, height);
      ctx.stroke();
    }

    // Draw beat offset indicator
    if (beatOffset > 0 && delaySeconds < ZOOM_WINDOW) {
      const offsetX = ((delaySeconds - (actualStart % delaySeconds)) / ZOOM_WINDOW) * width;
      if (offsetX >= 0 && offsetX <= width) {
        ctx.strokeStyle = 'rgba(255, 193, 7, 0.5)';
        ctx.lineWidth = 2;
        ctx.setLineDash([5, 5]);
        ctx.beginPath();
        ctx.moveTo(offsetX, 0);
        ctx.lineTo(offsetX, height);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }

    // Draw time labels
    ctx.fillStyle = '#999';
    ctx.font = '9px monospace';
    ctx.textAlign = 'left';
    ctx.fillText(`${actualStart.toFixed(1)}s`, 5, height - 5);
    ctx.textAlign = 'right';
    ctx.fillText(`${actualEnd.toFixed(1)}s`, width - 5, height - 5);
  }, [kicks, beatOffset, song2Bpm, currentTime]);

  return (
    <div style={{ marginBottom: '15px' }}>
      <canvas
        ref={canvasRef}
        style={{
          width: '100%',
          height: '500px',
          borderRadius: '8px',
          background: 'rgba(99, 102, 241, 0.05)',
          border: '1px solid rgba(99, 102, 241, 0.2)',
          display: 'block'
        }}
      />

      {/* Zoom Slider */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
        marginTop: '10px',
        padding: '10px',
        background: 'rgba(99, 102, 241, 0.05)',
        borderRadius: '6px',
        border: '1px solid rgba(99, 102, 241, 0.15)'
      }}>
        <label style={{ fontSize: '12px', color: '#999', minWidth: '50px' }}>🔍 Zoom:</label>
        <input
          type="range"
          min="3"
          max="30"
          step="1"
          value={zoomLevel}
          onChange={(e) => onZoomChange(parseInt(e.target.value))}
          style={{ flex: 1, cursor: 'pointer' }}
        />
        <span style={{ fontSize: '12px', color: '#aaa', minWidth: '50px', textAlign: 'right' }}>
          {zoomLevel}s
        </span>
      </div>

      {loading && (
        <div style={{ fontSize: '11px', color: '#666', marginTop: '5px', textAlign: 'center' }}>
          🎯 Selected stem waveforms ({zoomLevel}s zoom window)
        </div>
      )}
    </div>
  );
}
