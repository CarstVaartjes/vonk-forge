import { LifecycleState } from "../api/vocabulary.generated";
import type { ActivityFilters } from "../api/types";
import type { ActivityStatus } from "./activity-status";

export type ActivityView = "timeline" | "table";
export const VIEW_PREFERENCE_KEY = "vonk.activity.view";

/** The statuses a link can open the page on. */
const LINKABLE_STATUSES: readonly ActivityStatus[] = [
  "recorded",
  "in_progress",
  "attention",
  "unsuccessful",
  "called_off",
];

export const OPERATION_STATES = [
  "queued",
  "running",
  LifecycleState.BACKOFF,
  LifecycleState.OBSERVING,
  "waiting",
  LifecycleState.NEEDS_OPERATOR,
  "compensating",
  "uncertain",
  "failed",
  "cancelled",
  "superseded",
  "succeeded",
];
export const REQUEST_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** Filters live in the URL so a filtered view can be linked and reloaded, like `vonkctl fleet activity --state --target --request-id`. */
export function readFilters(): ActivityFilters {
  const params = new URLSearchParams(location.search);
  return {
    state: params.get("state") || undefined,
    target: params.get("target") || undefined,
    requestId: params.get("request_id") || undefined,
  };
}

/** `?status=unsuccessful` opens the page already filtered, so other pages can link a count to its events. */
export function readStatus(): ActivityStatus | "" {
  const value = new URLSearchParams(location.search).get("status");
  return LINKABLE_STATUSES.find((status) => status === value) ?? "";
}

export function writeFilters(filters: ActivityFilters): void {
  const params = new URLSearchParams(location.search);
  for (const [key, value] of [
    ["state", filters.state],
    ["target", filters.target],
    ["request_id", filters.requestId],
  ] as const) {
    if (value) params.set(key, value);
    else params.delete(key);
  }
  const query = params.toString();
  history.replaceState(history.state, "", `${location.pathname}${query ? `?${query}` : ""}`);
}

export function readViewPreference(): ActivityView {
  try {
    return localStorage.getItem(VIEW_PREFERENCE_KEY) === "table" ? "table" : "timeline";
  } catch {
    return "timeline";
  }
}
