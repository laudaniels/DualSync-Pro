import React, { useEffect, useRef, useState } from 'react';
import { stemLabels } from './stemConstants';

// Colors for each stem -- this component had its own copy of stemLabels too
// (a legacy 7-stem-only list, missing guitar/piano/multi-engine's 'other'
// wasn't the issue but guitar/piano were, showing up as "undefined" in the
// canvas), now replaced by the shared one from stemConstants.
const stemColors = {
  vocals: 'rgb(168, 85, 247)',    // purple
  kick: 'rgb(99, 102, 241)',      // indigo
  snare: 'rgb(236, 72, 153)',     // pink
  hihat: 'rgb(249, 115, 22)',     // orange
  tom: 'rgb(34, 197, 94)',        // green
  bass: 'rgb(59, 130, 246)',      // blue
  guitar: 'rgb(234, 179, 8)',     // yellow
  piano: 'rgb(20, 184, 166)',     // teal
  other: 'rgb(168, 162, 158)'     // gray
};

export default function Waveform({ kicks, currentTime, beatOffset, song2Bpm, song1Bpm = 120, song1BeatAnchor = 0, zoomLevel = 10, onZoomChange }) {
  const canvasRef = useRef(null);
  const lastSizeRef = useRef({ width: 0, height: 0 });
  const [loading, setLoading] = useState(true);
  const ZOOM_WINDOW = zoomLevel; // Dynamic zoom level

  useEffect(() => {
    if (!canvasRef.current) return;

    const canvas = canvasRef.current;
    const ctx = canvas.getContext('2d');
    const width = canvas.offsetWidth;
    const height = canvas.offsetHeight;

    // Resizing the backing store (and re-applying the DPR scale) resets the
    // canvas's transform, so only do it when the CSS size actually changed --
    // this redraw fires ~10x/sec from the playhead timer, and reallocating
    // the backing store every time was heavy enough to stall the main thread.
    if (lastSizeRef.current.width !== width || lastSizeRef.current.height !== height) {
      canvas.width = width * window.devicePixelRatio;
      canvas.height = height * window.devicePixelRatio;
      ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
      lastSizeRef.current = { width, height };
    }

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

    // Draw beat grid, aligned to Song 1's detected beat phase
    const beatInterval = song1Bpm > 0 ? 60 / song1Bpm : 0;
    if (beatInterval > 0) {
      const firstBeat = song1BeatAnchor + Math.ceil((actualStart - song1BeatAnchor) / beatInterval) * beatInterval;
      let beatIdx = Math.round((firstBeat - song1BeatAnchor) / beatInterval);

      for (let beatTime = firstBeat; beatTime <= actualEnd; beatTime += beatInterval, beatIdx++) {
        const x = ((beatTime - actualStart) / ZOOM_WINDOW) * width;
        const isBar = beatIdx % 4 === 0; // Emphasize downbeats (assumes 4/4)

        ctx.strokeStyle = isBar ? 'rgba(255, 255, 255, 0.18)' : 'rgba(255, 255, 255, 0.07)';
        ctx.lineWidth = isBar ? 1.5 : 1;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, height);
        ctx.stroke();
      }
    }

    // Draw each selected stem
    stemsToDisplay.forEach((stem, stemIdx) => {
      const rowTop = stemIdx * rowHeight;
      const yOffset = rowTop + rowHeight / 2;
      const laneHeight = rowHeight / stemsPerSlot; // one lane per song, within this stem's row
      const stemData = kicks.stems[stem];
      const color = stemColors[stem];

      // Draw both Song 1 and Song 2 for this stem
      [
        { data: stemData.data1, laneIdx: 0, label: `${stemLabels[stem]} (Song 1)`, xShift: 0 },
        { data: stemData.data2, laneIdx: 1, label: `Song 2`, xShift: song2PixelOffset }
      ].forEach((songData, songIdx) => {
        if (!songData.data) return;

        // Center of this song's own lane (half the stem's row each), with a
        // small margin so peaks don't run into the neighboring lane.
        const yPos = rowTop + (songData.laneIdx + 0.5) * laneHeight;
        const amplitudeScale = laneHeight * 0.9;
        // Individual stems rarely hit true full-scale (+/-1.0) peaks even at
        // their loudest -- a flat visual gain (clamped back to +/-1 before
        // scaling) makes quieter passages actually use the lane instead of
        // looking flat, without touching the real audio.
        const VISUAL_GAIN = 2.2;

        const { min: minArr, max: maxArr, bucketsPerSec } = songData.data;
        const totalBuckets = minArr.length;
        const startSample = Math.max(0, Math.floor(actualStart * bucketsPerSec));
        const endSample = Math.min(totalBuckets, Math.ceil(actualEnd * bucketsPerSec));
        const visibleBuckets = Math.max(1, endSample - startSample);
        // Aggregate (min/max) per on-screen pixel column rather than
        // connecting single sample points with straight lines -- this is
        // what keeps the waveform looking like a dense, filled envelope
        // (matching WaveformPreview.jsx's slider thumbnails) instead of a
        // sparse zigzag, whether zoomed in (few buckets per pixel) or
        // zoomed out (many buckets collapsed into one column).
        const bucketsPerPixel = visibleBuckets / width;

        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.globalAlpha = songIdx === 0 ? 1 : 0.6; // Song 2 slightly faded
        ctx.beginPath();

        for (let px = 0; px < width; px++) {
          const x = songData.xShift + px;
          if (x < 0 || x > width) continue;

          const bStart = startSample + Math.floor(px * bucketsPerPixel);
          const bEnd = Math.max(bStart + 1, startSample + Math.floor((px + 1) * bucketsPerPixel));
          let colMin = 0, colMax = 0;
          for (let b = bStart; b < bEnd && b < totalBuckets; b++) {
            if (minArr[b] < colMin) colMin = minArr[b];
            if (maxArr[b] > colMax) colMax = maxArr[b];
          }
          colMin = Math.max(-1, colMin * VISUAL_GAIN);
          colMax = Math.min(1, colMax * VISUAL_GAIN);

          const yMin = yPos + (colMin * amplitudeScale);
          const yMax = yPos + (colMax * amplitudeScale);
          ctx.moveTo(x, yMin);
          ctx.lineTo(x, yMax);
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
  }, [kicks, beatOffset, song2Bpm, song1Bpm, song1BeatAnchor, currentTime, ZOOM_WINDOW]);

  return (
    <div style={{ marginBottom: '15px' }}>
      <canvas
        ref={canvasRef}
        style={{
          width: '100%',
          height: '650px',
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
          max="200"
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
