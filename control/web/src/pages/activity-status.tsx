import {
  LIVE_LIFECYCLE_STATES,
  LifecycleState,
  TERMINAL_LIFECYCLE_STATES,
  UNSUCCESSFUL_LIFECYCLE_STATES,
} from "../api/vocabulary.generated";

/** A wait for a person. */
export function isOperatorWait(state: string | undefined): boolean {
  return state === LifecycleState.NEEDS_OPERATOR;
}
export type ActivityStatus =
  | "recorded"
  | "in_progress"
  | "attention"
  | "unsuccessful"
  | "called_off"
  | "unknown";
export type ActivitySummary = {
  request_id: string;
  actor: string;
  action: string;
  targets: string[];
};
/** The lifecycle words, grouped by the contract's sets: a cancel is the operator's own request, not a failure. */
function lifecycleStatus(state: string): ActivityStatus | undefined {
  if (UNSUCCESSFUL_LIFECYCLE_STATES.has(state)) return "unsuccessful";
  if (state === LifecycleState.CANCELLED) return "called_off";
  if (state === LifecycleState.NEEDS_OPERATOR) return "attention";
  if (TERMINAL_LIFECYCLE_STATES.has(state)) return "recorded";
  if (LIVE_LIFECYCLE_STATES.has(state)) return "in_progress";
  return undefined;
}

/** The states of other operation kinds that are not lifecycle words. */
const OPERATION_STATUS: Record<string, ActivityStatus> = {
  canceled: "called_off",
  error: "unsuccessful",
  expired: "unsuccessful",
  uncertain: "attention",
  waiting: "in_progress",
  compensating: "in_progress",
  pending: "in_progress",
  planned: "in_progress",
  starting: "in_progress",
  stopping: "in_progress",
  compensated: "recorded",
  completed: "recorded",
};

/** An operation state as the Activity page classes it; a state not listed here is not known. */
function operationStatus(state: string): ActivityStatus {
  return lifecycleStatus(state) ?? OPERATION_STATUS[state] ?? "unknown";
}

/** Finished work does not change again; every other state (waiting, uncertain, not known) can. */
export function operationEnded(state: string): boolean {
  const status = operationStatus(state);
  return status === "unsuccessful" || status === "called_off" || status === "recorded";
}

export function activityStatus(event: ActivitySummary): ActivityStatus {
  if (event.action.startsWith("operation."))
    return operationStatus(event.action.split(".").at(-1) ?? "");
  if (/(?:^|\.)(?:failed|rejected|throttled|denied|error)(?:\.|$)/.test(event.action))
    return "unsuccessful";
  if (/(?:^|\.)(?:uncertain|warning|conflict|stale)(?:\.|$)/.test(event.action)) return "attention";
  return "recorded";
}

export function statusLabel(status: ActivityStatus): string {
  if (status === "unsuccessful") return "Unsuccessful";
  if (status === "called_off") return "Cancelled";
  if (status === "attention") return "Needs review";
  if (status === "in_progress") return "In progress";
  if (status === "unknown") return "Unknown state";
  return "Recorded";
}

function statusTone(status: ActivityStatus): "neutral" | "ok" | "warn" | "bad" | "info" {
  if (status === "unsuccessful") return "bad";
  if (status === "attention") return "warn";
  if (status === "in_progress") return "info";
  if (status === "unknown" || status === "called_off") return "neutral";
  return "ok";
}
export { statusTone };

const LIVE_JOB_STATES = new Set<string>([
  LifecycleState.BACKOFF,
  LifecycleState.OBSERVING,
  "compensating",
  "pending",
  "planned",
  "queued",
  "running",
  "starting",
  "stopping",
  "waiting",
]);

/** The one decision "this card polls itself": live work needs no Refresh button. */
export function stateUpdatesItself(state: string): boolean {
  return LIVE_JOB_STATES.has(state);
}
