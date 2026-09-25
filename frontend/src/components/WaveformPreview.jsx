import React, { useEffect, useRef } from 'react';

// Stem colors (matched with Waveform.jsx player)
const STEM_COLORS = {
  vocals: 'rgb(168, 85, 247)',    // purple
  kick: 'rgb(99, 102, 241)',      // indigo
  snare: 'rgb(236, 72, 153)',     // pink
  hihat: 'rgb(249, 115, 22)',     // orange
  tom: 'rgb(34, 197, 94)',        // green
  bass: 'rgb(59, 130, 246)',      // blue
  guitar: 'rgb(168, 85, 247)',    // purple (new)
  piano: 'rgb(6, 182, 212)',      // cyan (new)
  other: 'rgb(168, 162, 158)'     // gray
};

/**
 * WaveformPreview: Render a tiny waveform thumbnail for a stem WAV file.
 * Shows audio data as a line graph on canvas (120x40px).
 *
 * Props:
 *   - stemPath: URL/path to the WAV file
 *   - stemName: Name of stem (for color lookup)
 *   - width: Canvas width (default 120)
 *   - height: Canvas height (default 40)
 */
function WaveformPreview({ stemPath, stemName, width = 120, height = 40 }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    if (!stemPath || !canvasRef.current) return;

    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // Set canvas size
    canvas.width = width;
    canvas.height = height;

    // Load and decode audio
    const loadAndDraw = async () => {
      try {
        const response = await fetch(stemPath);
        const arrayBuffer = await response.arrayBuffer();
        const audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const audioBuffer = await audioContext.decodeAudioData(arrayBuffer);

        // Get audio data
        const rawData = audioBuffer.getChannelData(0);

        // Draw waveform
        drawWaveform(ctx, rawData, canvas.width, canvas.height, stemName);
      } catch (error) {
        // Silently fail on audio decode errors
        console.debug(`Could not load waveform for ${stemName}:`, error.message);
        drawEmpty(ctx, canvas.width, canvas.height);
      }
    };

    loadAndDraw();
  }, [stemPath, stemName, width, height]);

  return (
    <canvas
      ref={canvasRef}
      width={width}
      height={height}
      style={{
        display: 'block',
        background: 'rgba(0, 0, 0, 0.3)',
        borderRadius: '3px',
        border: '1px solid rgba(255, 255, 255, 0.1)'
      }}
    />
  );
}

/**
 * Draw waveform line on canvas
 */
function drawWaveform(ctx, rawData, width, height, stemName) {
  const centerY = height / 2;
  const color = STEM_COLORS[stemName] || STEM_COLORS.other;
  const samplesPerPixel = Math.ceil(rawData.length / width);

  // Clear canvas
  ctx.fillStyle = 'rgba(0, 0, 0, 0.3)';
  ctx.fillRect(0, 0, width, height);

  // Draw waveform line
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.2;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.beginPath();

  for (let i = 0; i < width; i++) {
    const start = i * samplesPerPixel;
    const end = Math.min(start + samplesPerPixel, rawData.length);

    // Find min/max in this pixel's chunk
    let min = 0, max = 0;
    for (let j = start; j < end; j++) {
      const sample = rawData[j];
      if (sample < min) min = sample;
      if (sample > max) max = sample;
    }

    // Scale to canvas height
    const minY = centerY - (min * centerY);
    const maxY = centerY - (max * centerY);

    if (i === 0) {
      ctx.moveTo(i, minY);
    } else {
      // Draw both min and max points for better resolution
      if (Math.abs(maxY - minY) > 0.5) {
        ctx.lineTo(i, maxY);
        ctx.lineTo(i, minY);
      } else {
        ctx.lineTo(i, (minY + maxY) / 2);
      }
    }
  }

  ctx.stroke();
}

/**
 * Draw empty state (no audio data)
 */
function drawEmpty(ctx, width, height) {
  ctx.fillStyle = 'rgba(0, 0, 0, 0.3)';
  ctx.fillRect(0, 0, width, height);

  // Draw a simple empty indicator
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.2)';
  ctx.lineWidth = 0.8;
  ctx.setLineDash([2, 2]);
  ctx.strokeRect(1, 1, width - 2, height - 2);
  ctx.setLineDash([]);
}

export default WaveformPreview;
