// Web Audio buffer-based playback engine for the dual-song stem mixer.
//
// Why this exists: the previous design used 14 independent <audio> elements
// (7 stems x 2 songs), each holding its own long-lived streaming HTTP
// connection for the whole playback session. Browsers cap concurrent
// connections per origin at 6 for HTTP/1.1 -- confirmed with Playwright
// network tracing that exactly 6 of the 14 requests ever got a response, the
// other 8 sat forever waiting for a connection slot that never freed up
// (each of the 6 active ones stays open streaming for the entire song). No
// server-side fix (threaded Werkzeug, waitress, removing logging overhead)
// changes that; it's a browser-side connection limit.
//
// This engine instead fetches each stem's file ONCE, fully, and decodes it
// into an in-memory AudioBuffer. Playback is then driven by AudioBufferSourceNodes
// scheduled against one shared AudioContext clock -- no long-lived streaming
// connections during playback at all, and (as a bonus) all 14 sources start
// at the exact same context time and loop at the exact same reference length,
// so they cannot drift apart the way independent <audio> elements could.
export class DualStemPlayer {
  constructor() {
    this.audioContext = null;
    this.delayNode = null; // Song 2 routes through this for the beat-offset slider
    this.buffers = { 0: {}, 1: {} }; // buffers[slot][stem] -> AudioBuffer
    this.gainNodes = { 0: {}, 1: {} }; // gainNodes[slot][stem] -> GainNode (created once, kept across reprocessing)
    this.sourceNodes = { 0: {}, 1: {} }; // sourceNodes[slot][stem] -> currently-playing AudioBufferSourceNode or null

    this.playing = false;
    this.startContextTime = 0; // audioContext.currentTime when the current play() started
    this.startOffset = 0; // playback position (seconds) at that moment

    this.volumes = { 0: {}, 1: {} }; // per-stem 0-1
    this.crossfader = 50; // 0-100, 0=song1 only, 100=song2 only
    this.beatOffsetSeconds = 0;
  }

  // Must be called (or re-called) from inside a real user-gesture handler --
  // creating/resuming an AudioContext outside one is blocked by browsers.
  ensureContext() {
    if (!this.audioContext) {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      this.audioContext = ctx;
      this.delayNode = ctx.createDelay(8); // max 8 beats worth of offset
      this.delayNode.connect(ctx.destination);
    }
    if (this.audioContext.state === 'suspended') {
      this.audioContext.resume();
    }
    return this.audioContext;
  }

  _ensureGainNode(slot, stem) {
    if (this.gainNodes[slot][stem]) return this.gainNodes[slot][stem];
    const ctx = this.ensureContext();
    const gain = ctx.createGain();
    if (slot === 1) {
      gain.connect(this.delayNode);
    } else {
      gain.connect(ctx.destination);
    }
    this.gainNodes[slot][stem] = gain;
    return gain;
  }

  // Fetches and decodes one stem's audio file. Safe to call again for the
  // same slot/stem (e.g. after BPM/key reprocessing swaps in a new file) --
  // replaces the buffer, keeps the same GainNode and its connections.
  async loadStem(slot, stem, url) {
    const ctx = this.ensureContext();
    const res = await fetch(url);
    if (!res.ok) throw new Error(`Fetch failed for ${url}: ${res.status}`);
    const arrayBuffer = await res.arrayBuffer();
    const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
    this.buffers[slot][stem] = audioBuffer;
    this._ensureGainNode(slot, stem);
    this.applyGain(slot, stem);
    return audioBuffer;
  }

  isSlotReady(slot, stemNames) {
    return stemNames.every(stem => !!this.buffers[slot][stem]);
  }

  // Song 1's vocals define the mashup's loop length, matching the original
  // <audio>-element design (the whole mashup restarted whenever Song 1's
  // vocals reached its own end, regardless of Song 2's length).
  getReferenceDuration() {
    return this.buffers[0]?.vocals?.duration || 0;
  }

  setVolume(slot, stem, value) {
    this.volumes[slot][stem] = value;
    this.applyGain(slot, stem);
  }

  setCrossfader(value) {
    this.crossfader = value;
    for (const slot of [0, 1]) {
      for (const stem of Object.keys(this.gainNodes[slot])) {
        this.applyGain(slot, stem);
      }
    }
  }

  applyGain(slot, stem) {
    const gainNode = this.gainNodes[slot]?.[stem];
    if (!gainNode || !this.audioContext) return;
    const crossfadePercent = this.crossfader / 100;
    const masterVol = slot === 0 ? 1 - crossfadePercent : crossfadePercent;
    const stemVol = this.volumes[slot]?.[stem] ?? 1.0;
    const target = masterVol * stemVol;
    const now = this.audioContext.currentTime;
    // Tiny ramp instead of an instant jump -- avoids an audible click on drag.
    gainNode.gain.cancelScheduledValues(now);
    gainNode.gain.setValueAtTime(gainNode.gain.value, now);
    gainNode.gain.linearRampToValueAtTime(target, now + 0.02);
  }

  setBeatOffset(beats, song2Bpm) {
    this.beatOffsetSeconds = beats > 0 ? (beats / (song2Bpm || 120)) * 60 : 0;
    if (this.delayNode && this.audioContext) {
      const clamped = Math.max(0, Math.min(this.beatOffsetSeconds, 8));
      this.delayNode.delayTime.setValueAtTime(clamped, this.audioContext.currentTime);
    }
  }

  // Starts every loaded stem across both songs at the exact same future
  // context time, looping at Song 1 vocals' length -- sample-accurate sync,
  // no per-element drift possible.
  play(stemNames) {
    const ctx = this.ensureContext();
    const referenceDuration = this.getReferenceDuration();
    const lookahead = 0.1; // small future offset so all sources schedule in sync
    const startAt = ctx.currentTime + lookahead;
    const offset = referenceDuration > 0 ? this.startOffset % referenceDuration : this.startOffset;

    for (const slot of [0, 1]) {
      for (const stem of stemNames) {
        const buffer = this.buffers[slot][stem];
        const gainNode = this.gainNodes[slot][stem];
        if (!buffer || !gainNode) continue;

        const source = ctx.createBufferSource();
        source.buffer = buffer;
        if (referenceDuration > 0) {
          source.loop = true;
          source.loopStart = 0;
          source.loopEnd = referenceDuration;
        }
        source.connect(gainNode);
        // Starting past a stem's own (shorter) buffer just plays silence for
        // the remainder of this cycle -- harmless, and the next loop wraps
        // it back to loopStart with everything else.
        source.start(startAt, Math.min(offset, buffer.duration));
        this.sourceNodes[slot][stem] = source;
      }
    }

    this.startContextTime = startAt;
    this.playing = true;
  }

  pause() {
    if (!this.playing) return;
    this.startOffset = this.getPosition();
    for (const slot of [0, 1]) {
      for (const stem of Object.keys(this.sourceNodes[slot])) {
        const source = this.sourceNodes[slot][stem];
        if (source) {
          try { source.stop(); } catch (e) { /* already stopped */ }
        }
        this.sourceNodes[slot][stem] = null;
      }
    }
    this.playing = false;
  }

  // Absolute position (seconds) within the current loop cycle.
  getPosition() {
    const referenceDuration = this.getReferenceDuration();
    if (!this.audioContext) return this.startOffset;
    if (!this.playing) return this.startOffset;
    const elapsed = this.audioContext.currentTime - this.startContextTime;
    const raw = this.startOffset + Math.max(0, elapsed);
    return referenceDuration > 0 ? raw % referenceDuration : raw;
  }

  seek(offsetSeconds, stemNames) {
    const wasPlaying = this.playing;
    if (wasPlaying) this.pause();
    const referenceDuration = this.getReferenceDuration();
    this.startOffset = referenceDuration > 0
      ? Math.max(0, Math.min(offsetSeconds, referenceDuration))
      : Math.max(0, offsetSeconds);
    if (wasPlaying) this.play(stemNames);
  }

  // Full teardown, e.g. on cleanup -- next loadStem()/play() call recreates
  // everything from scratch.
  reset() {
    this.pause();
    if (this.audioContext) {
      this.audioContext.close().catch(() => {});
    }
    this.audioContext = null;
    this.delayNode = null;
    this.buffers = { 0: {}, 1: {} };
    this.gainNodes = { 0: {}, 1: {} };
    this.sourceNodes = { 0: {}, 1: {} };
    this.startContextTime = 0;
    this.startOffset = 0;
  }
}
