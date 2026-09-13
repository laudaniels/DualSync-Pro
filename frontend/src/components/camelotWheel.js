// Camelot Wheel helpers for harmonic-mixing-aware key recommendations.
//
// The Camelot Wheel renumbers the 24 major/minor keys (1A-12B) around the
// circle of fifths so "compatible" moves are visually obvious: the same
// code, an adjacent number (same letter), or the same number with the other
// letter (relative major/minor) all sound harmonically natural together.
// The key insight used here: moving by a perfect fifth is the same musical
// interval whether a track is major or minor, so "how many fifths apart"
// two pitch classes are is mode-independent -- exactly what we need to rank
// how big a stretch a given target key is for a track, regardless of
// whether that track's own mode was even detected.

export const KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

// Camelot number for each pitch class (0=C..11=B), indexed by mode.
const CAMELOT_NUMBER = {
  major: [8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1],
  minor: [5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10]
};

export function keyNameToPitchClass(keyName) {
  return KEYS.indexOf(keyName);
}

// e.g. camelotCode('A', 'minor') -> '8A'. Defaults to major when the mode
// wasn't detected (Librosa alone can't determine it), clearly the more
// common/expected case for a bare key name.
export function camelotCode(keyName, scale) {
  const pc = keyNameToPitchClass(keyName);
  if (pc < 0) return null;
  const mode = scale === 'minor' ? 'minor' : 'major';
  const number = CAMELOT_NUMBER[mode][pc];
  return `${number}${mode === 'minor' ? 'A' : 'B'}`;
}

// Circular distance in perfect-fifth steps (0-6) between two pitch classes.
// Mode-independent: this is the number of Camelot-wheel hops a track has to
// move to reach the other pitch class, whether it's major or minor.
export function fifthsDistance(pcA, pcB) {
  if (pcA < 0 || pcB < 0) return null;
  let steps = (7 * (pcB - pcA)) % 12;
  if (steps < 0) steps += 12;
  if (steps > 6) steps -= 12;
  return Math.abs(steps);
}

// True Camelot-wheel distance between two (key, scale) pairs, mode included:
// 0 = identical code (already the same key), 1 = adjacent number (same
// letter, a fifth apart) or same number with the other letter (relative
// major/minor) -- the two classic "safe" harmonic-mixing moves. This is
// what decides whether two songs are ALREADY compatible as they stand,
// unlike fifthsDistance (mode-independent, used for ranking a shared target
// key), since here the two songs can be in different modes/keys and mode
// absolutely matters for whether that's a compatible pairing.
export function camelotDistanceBetween(keyA, scaleA, keyB, scaleB) {
  const pcA = keyNameToPitchClass(keyA);
  const pcB = keyNameToPitchClass(keyB);
  if (pcA < 0 || pcB < 0) return null;
  const modeA = scaleA === 'minor' ? 'minor' : 'major';
  const modeB = scaleB === 'minor' ? 'minor' : 'major';
  const numA = CAMELOT_NUMBER[modeA][pcA];
  const numB = CAMELOT_NUMBER[modeB][pcB];
  const numDist = Math.min(Math.abs(numA - numB), 12 - Math.abs(numA - numB));
  return modeA === modeB ? numDist : numDist + 1;
}

const TIER_BY_SCORE = {
  0: { label: 'Very good', emoji: '🟢' },
  1: { label: 'Very good', emoji: '🟢' },
  2: { label: 'Good', emoji: '👍' },
  3: { label: 'Fair', emoji: '🙂' },
  4: { label: 'Ok', emoji: '🆗' }
};

// Rank every candidate absolute key (0-11) by how big a circle-of-fifths
// stretch it is for the more-distant of the two songs, and return up to
// `limit` of the best, each labeled from "Very good" down to "Ok".
// Candidates scoring worse than 4 fifths for either song are dropped
// entirely rather than padded in, since they aren't a good harmonic match.
export function getKeyRecommendations(song1Key, song1Scale, song2Key, song2Scale, limit = 5) {
  const pc1 = keyNameToPitchClass(song1Key);
  const pc2 = keyNameToPitchClass(song2Key);
  if (pc1 < 0 || pc2 < 0) return [];

  const candidates = KEYS.map((candidateKey, pcX) => {
    const dist1 = fifthsDistance(pc1, pcX);
    const dist2 = fifthsDistance(pc2, pcX);
    const score = Math.max(dist1, dist2);
    const sameMode = !!song1Scale && !!song2Scale && song1Scale === song2Scale;
    return {
      key: candidateKey,
      score,
      dist1,
      dist2,
      combinedDist: dist1 + dist2,
      sameMode,
      camelot1: camelotCode(candidateKey, song1Scale),
      camelot2: camelotCode(candidateKey, song2Scale),
      ...TIER_BY_SCORE[score]
    };
  }).filter(c => c.score <= 4);

  candidates.sort((a, b) =>
    a.score - b.score ||
    (a.sameMode === b.sameMode ? 0 : a.sameMode ? -1 : 1) ||
    a.combinedDist - b.combinedDist
  );

  return candidates.slice(0, limit);
}
