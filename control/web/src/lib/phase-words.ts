import { PHASE_WORDS, WAIT_WORDS, WaitReason } from "../api/vocabulary.generated";

/** A progress phase in plain words, from the one generated table; an unknown phase shows as sent. */
export function phaseWords(phase: string): string {
  return PHASE_WORDS[phase] ?? phase;
}

/** Why work waits: the step for a pending dependency, the plain words for a known wait, else the code. */
export function waitWords(code: string, phase?: string): string {
  if (code === WaitReason.DEPENDENCY_PENDING && phase) {
    const words = phaseWords(phase);
    return words.charAt(0).toLowerCase() + words.slice(1);
  }
  return WAIT_WORDS[code] ?? code;
}
