import React, { useEffect, useRef, useState } from 'react';

export default function Waveform({ kicks, currentTime, beatOffset, song2Bpm }) {
  const canvasRef = useRef(null);
  const [loading, setLoading] = useState(true);
  const ZOOM_WINDOW = 15; // 15 seconds visible at a time

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

    if (!kicks || !kicks.data1 || !kicks.data2) {
      ctx.fillStyle = '#666';
      ctx.font = '12px monospace';
      ctx.textAlign = 'center';
      ctx.fillText('Loading kick waveforms...', width / 2, height / 2);
      return;
    }

    setLoading(false);

    // Calculate zoom window around currentTime
    const windowStart = Math.max(0, currentTime - ZOOM_WINDOW / 2);
    const windowEnd = windowStart + ZOOM_WINDOW;
    const totalDuration = kicks.duration || 10;

    // Clamp window to available data
    const actualStart = Math.min(windowStart, totalDuration - ZOOM_WINDOW);
    const actualEnd = actualStart + ZOOM_WINDOW;

    // Calculate sample indices for zoom window
    const totalSamples = kicks.data1.length;
    const startSample = Math.floor((actualStart / totalDuration) * totalSamples);
    const endSample = Math.floor((actualEnd / totalDuration) * totalSamples);
    const visibleSamples = endSample - startSample;

    // Calculate beat offset delay in seconds
    const delaySeconds = beatOffset > 0 ? (beatOffset / song2Bpm) * 60 : 0;

    // Draw kick waveform
    const drawKickWaveform = (data, yOffset, color, label) => {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();

      const pixelsPerSample = width / visibleSamples;
      for (let i = 0; i < visibleSamples; i++) {
        const sampleIdx = startSample + i;
        if (sampleIdx >= data.length) break;

        const x = i * pixelsPerSample;
        const amplitude = data[sampleIdx] || 0;
        const y = yOffset + (amplitude * (height / 3.5));

        if (i === 0) {
          ctx.moveTo(x, y);
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();

      // Draw label
      ctx.fillStyle = color;
      ctx.font = 'bold 11px monospace';
      ctx.textAlign = 'left';
      ctx.fillText(label, 10, yOffset - 5);

      // Draw center line
      ctx.strokeStyle = 'rgba(99, 102, 241, 0.1)';
      ctx.beginPath();
      ctx.moveTo(0, yOffset);
      ctx.lineTo(width, yOffset);
      ctx.stroke();
    };

    // Draw both kicks
    drawKickWaveform(kicks.data1, height / 4, 'rgb(99, 102, 241)', '🔊 Song 1 Kick');
    drawKickWaveform(kicks.data2, (3 * height) / 4, 'rgb(34, 197, 94)', '🔊 Song 2 Kick');

    // Draw playhead (current time)
    const playheadPercent = (currentTime - actualStart) / ZOOM_WINDOW;
    if (playheadPercent >= 0 && playheadPercent <= 1) {
      const playheadX = playheadPercent * width;
      ctx.strokeStyle = 'rgba(255, 87, 87, 0.8)';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(playheadX, 0);
      ctx.lineTo(playheadX, height);
      ctx.stroke();

      // Playhead label
      ctx.fillStyle = 'rgba(255, 87, 87, 0.8)';
      ctx.font = '10px monospace';
      ctx.textAlign = 'center';
      ctx.fillText('▶', playheadX, 15);
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

        ctx.fillStyle = 'rgba(255, 193, 7, 0.7)';
        ctx.font = '9px monospace';
        ctx.textAlign = 'center';
        ctx.fillText(`+${beatOffset}b`, offsetX, 30);
      }
    }

    // Draw time labels
    ctx.fillStyle = '#999';
    ctx.font = '10px monospace';
    ctx.textAlign = 'left';
    ctx.fillText(`${actualStart.toFixed(1)}s`, 5, height - 5);
    ctx.textAlign = 'right';
    ctx.fillText(`${actualEnd.toFixed(1)}s`, width - 5, height - 5);

    // Draw center time
    ctx.textAlign = 'center';
    const centerTime = actualStart + ZOOM_WINDOW / 2;
    ctx.fillText(`${centerTime.toFixed(1)}s`, width / 2, height - 5);
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
      {loading && (
        <div style={{ fontSize: '11px', color: '#666', marginTop: '5px', textAlign: 'center' }}>
          🎯 Kick waveforms zoomed to 15s window
        </div>
      )}
    </div>
  );
}
