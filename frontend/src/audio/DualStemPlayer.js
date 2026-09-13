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

    // Continuous drift correction: even a "beatmatched" Song 2 only
    // converges to within ~0.5 BPM of Song 1 (see time_stretch_audio's
    // tolerance) -- close enough that the static beat offset above looks
    // right at first, but the residual tempo error keeps compounding for as
    // long as the loop plays, drifting the two beat grids apart again
    // within every loop cycle (they only re-snap at the loop wrap). This
    // periodically measures that drift from each song's own bpm/beat-anchor
    // and nudges ONLY Song 2's live AudioBufferSourceNode.playbackRate by a
    // tiny amount to pull it back -- inaudible as a pitch change at this
    // magnitude, unlike a hard reseek which would click/jump.
    this.beatGrid = { 0: null, 1: null }; // beatGrid[slot] -> { bpm, anchor } or null
    this.driftCorrectionStrength = 0.5; // 0 (off) - 1 (max correction)
    this.driftCorrectionInterval = null;
    this.driftCorrectionIntervalMs = 200;

    // Reporting only (not used for correction itself): how much drift is
    // showing right now, and the running total of how much Song 2's timing
    // has actually been nudged since playback began -- i.e. hard evidence
    // of how far the two songs' native tempos really disagree, not just
    // instantaneous noise.
    this.lastDriftMs = 0;
    this.cumulativeCorrectionBeats = 0;
  }

  // Must be called (or re-called) from inside a real user-gesture handler --
  // creating/resuming an AudioContext outside one is blocked by browsers.
  ensureContext() {
    if (!this.audioContext) {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      this.audioContext = ctx;
      // The Web Audio API spec requires maxDelayTime to be STRICTLY LESS
      // THAN 180 seconds ("(0, 180)" is an open interval -- 180 itself also
      // throws NotSupportedError), regardless of how much headroom the
      // beat-offset range might want. 179 is comfortably inside that. At
      // very slow tempos (below ~42 BPM) the full 32-bar range can't quite
      // be reached live -- an acceptable edge case for a rare tempo.
      // Falling back to a small, universally-safe value on any failure here
      // (rather than letting it throw) matters a lot: this.audioContext is
      // already assigned above, so on a throw here delayNode would stay
      // permanently null for the rest of the session -- and since Song 2's
      // gain nodes ALWAYS route through delayNode (see _ensureGainNode),
      // every one of them would then fail to .connect(null), silently
      // breaking Song 2's entire audio path (this exact bug shipped once
      // already, from createDelay(200) exceeding the cap).
      try {
        this.delayNode = ctx.createDelay(179);
      } catch (e) {
        console.error('createDelay(179) failed, falling back to a smaller max delay:', e);
        this.delayNode = ctx.createDelay(10);
      }
      // NOT connected here -- see play()/pause(). A DelayNode is a FIFO: at
      // a large beat offset, samples pushed into it seconds ago are still
      // queued up to come out later, regardless of whether the source that
      // originally fed it has since been stopped. Just calling stop() on
      // Song 2's sources (as pause() does) leaves all of that already-queued
      // audio to keep draining out for the full delay duration -- so at a
      // 16-bar offset, pausing looked like Song 2 kept playing for another
      // 16 bars. Disconnecting the delay node's OUTPUT on pause silences it
      // immediately regardless of what's still queued inside it.
      this.delayNodeConnected = false;
    }
    if (this.audioContext.state === 'suspended') {
      this.audioContext.resume();
    }
    return this.audioContext;
  }

  _connectDelayNode() {
    if (this.delayNode && !this.delayNodeConnected) {
      this.delayNode.connect(this.audioContext.destination);
      this.delayNodeConnected = true;
    }
  }

  _disconnectDelayNode() {
    if (this.delayNode && this.delayNodeConnected) {
      this.delayNode.disconnect();
      this.delayNodeConnected = false;
    }
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
      const clamped = Math.max(0, Math.min(this.beatOffsetSeconds, 179));
      this.delayNode.delayTime.setValueAtTime(clamped, this.audioContext.currentTime);
    }
  }

  // bpm/anchor as currently playing (post any beatmatch/transpose, not the
  // original detected values) -- anchor is a position in SECONDS within
  // that song's own buffer where its first detected beat lands.
  setBeatGridInfo(slot, bpm, anchor) {
    this.beatGrid[slot] = (bpm && anchor != null) ? { bpm, anchor } : null;
  }

  // 0 (off) to 1 (max correction). Safe to call anytime, including mid-playback.
  setDriftCorrectionStrength(strength) {
    this.driftCorrectionStrength = Math.max(0, Math.min(1, strength));
  }

  _wrapPhase(value, period) {
    if (!period) return 0;
    let phase = value % period;
    if (phase < 0) phase += period;
    return phase;
  }

  // Runs periodically while playing: measures how far Song 2's beat grid
  // has drifted from Song 1's (using each song's own bpm/anchor, projected
  // through the shared loop position and the static beat-offset delay), and
  // nudges Song 2's live sources' playbackRate proportionally to pull it
  // back -- a proportional controller, so it naturally settles to rate=1
  // once the drift is corrected rather than needing an explicit "done" step.
  _correctDrift() {
    if (!this.playing || this.driftCorrectionStrength <= 0) return;
    const grid0 = this.beatGrid[0];
    const grid1 = this.beatGrid[1];
    if (!grid0 || !grid1) return;

    const period0 = 60 / grid0.bpm;
    const period1 = 60 / grid1.bpm;
    if (!period0 || !period1) return;

    const pos = this.getPosition();
    const phase0 = this._wrapPhase(pos - grid0.anchor, period0);
    // Song 2's AUDIBLE phase lags its buffer phase by the static beat-offset
    // delay (that's the whole point of the delay node), so subtract it here
    // to compare like-for-like "what's actually audible right now."
    const phase1 = this._wrapPhase(pos - this.beatOffsetSeconds - grid1.anchor, period1);

    // Shortest signed distance (in seconds) from phase1 to phase0, using
    // song2's own beat period as the wraparound reference. Positive means
    // Song 2 is lagging (needs to speed up to catch phase0).
    let drift = phase0 - phase1;
    if (drift > period1 / 2) drift -= period1;
    if (drift < -period1 / 2) drift += period1;

    const deadband = 0.002; // 2ms -- ignore sub-perceptible jitter
    const maxRateOffset = 0.005 * this.driftCorrectionStrength; // up to +/-0.5% at max strength
    const kP = 0.15; // proportional gain

    const rateOffset = Math.abs(drift) > deadband
      ? Math.max(-maxRateOffset, Math.min(maxRateOffset, drift * kP))
      : 0;

    // Reporting: current drift, and the actual amount of Song 2 playback
    // time this tick's rate nudge added or removed, in beats (of Song 2's
    // own tempo) -- accumulated, this is real evidence of how much ongoing
    // correction has been needed, not just this instant's noise.
    this.lastDriftMs = drift * 1000;
    const tickSeconds = this.driftCorrectionIntervalMs / 1000;
    this.cumulativeCorrectionBeats += Math.abs(rateOffset * tickSeconds) / period1;

    const now = this.audioContext.currentTime;
    for (const stem of Object.keys(this.sourceNodes[1])) {
      const source = this.sourceNodes[1][stem];
      if (source) {
        source.playbackRate.setTargetAtTime(1 + rateOffset, now, 0.3);
      }
    }
  }

  // Starts every loaded stem across both songs at the exact same future
  // context time, looping at Song 1 vocals' length -- sample-accurate sync,
  // no per-element drift possible.
  play(stemNames) {
    const ctx = this.ensureContext();
    this._connectDelayNode();
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

    clearInterval(this.driftCorrectionInterval);
    this.driftCorrectionInterval = setInterval(() => this._correctDrift(), this.driftCorrectionIntervalMs);
  }

  // { instantaneousMs, cumulativeBeats } for display -- see the comment on
  // the constructor fields for what each one actually means.
  getDriftInfo() {
    return { instantaneousMs: this.lastDriftMs, cumulativeBeats: this.cumulativeCorrectionBeats };
  }

  pause() {
    if (!this.playing) return;
    clearInterval(this.driftCorrectionInterval);
    this.driftCorrectionInterval = null;
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
    this._disconnectDelayNode();
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
    this.beatGrid = { 0: null, 1: null };
    this.lastDriftMs = 0;
    this.cumulativeCorrectionBeats = 0;
  }
}
