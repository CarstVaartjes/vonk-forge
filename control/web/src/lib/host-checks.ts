import type { VisualFleetNode } from "../api/types";

export type HostCheckAdvice = NonNullable<VisualFleetNode["host_checks"]>[number];

export const UNDO_LABEL = "Undo:";

/**
 * One host check as lines, shared with `vonkctl fleet detail` through
 * `tests/fixtures/fleet-wording-host-checks.json`: the Controller's finding and explanation, then each
 * command on its own line, then `Undo:` and each undo command, in order. The Controller owns every word.
 */
export function hostCheckLines(advice: HostCheckAdvice): string[] {
  return [
    `${advice.finding} ${advice.explanation}`,
    ...advice.commands,
    ...(advice.undo?.length ? [UNDO_LABEL, ...advice.undo] : []),
  ];
}
