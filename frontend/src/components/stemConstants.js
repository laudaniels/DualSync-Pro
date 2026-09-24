// Legacy 7-stem set (drums → kick, snare, hihat, tom). Kept as the fallback
// used before any song has actually loaded stems -- once a song is loaded,
// components should use orderStems() on that song's real stem keys instead,
// since multi-engine mode (DUALSYNC_MULTI_ENGINE) produces 9 stems including
// guitar/piano that aren't in this list.
export const stemNames = ['vocals', 'kick', 'snare', 'hihat', 'tom', 'bass', 'other'];

export const stemLabels = {
  vocals: '🎤 Vocals',
  kick: '🔊 Kick',
  snare: '🥁 Snare',
  hihat: '⚡ Hi-Hat',
  tom: '🔔 Tom',
  bass: '🎸 Bass',
  guitar: '🎸 Guitar',
  piano: '🎹 Piano',
  other: '🎛️ Other'
};

// Canonical display/playback order for whichever stem keys a song actually
// has (7 in legacy mode, 9 in multi-engine mode). Filters out anything not
// present, and appends any unrecognized key rather than silently dropping it.
const STEM_PRIORITY = ['vocals', 'kick', 'snare', 'hihat', 'tom', 'bass', 'guitar', 'piano', 'other'];

export function orderStems(stemKeys) {
  const keys = new Set(stemKeys);
  const ordered = STEM_PRIORITY.filter(name => keys.has(name));
  for (const name of stemKeys) {
    if (!ordered.includes(name)) ordered.push(name);
  }
  return ordered;
}
