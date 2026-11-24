import { ApiError } from "../../api/client";
import {
  ProfileAction,
  ProfileLoadAction,
  TERMINAL_LIFECYCLE_STATES,
} from "../../api/vocabulary.generated";
import type {
  ControlApi,
  FleetProfileNumber,
  FleetProfileApplicationView,
  FleetProfilePreview,
} from "../../api/types";

export type PendingProfileLoad = { requestKey: string; reviewedEffectsDigest: string };

/** A superseded load that names its successor is not an end: the latest-load observation continues under the successor. */
export const ended = (application: Pick<FleetProfileApplicationView, "state" | "superseded_by">) =>
  TERMINAL_LIFECYCLE_STATES.has(application.state) &&
  !(application.state === "superseded" && application.superseded_by);

function isAmbiguousLoadFailure(error: unknown): boolean {
  return error instanceof TypeError || (error instanceof ApiError && error.status >= 500);
}

export async function submitProfileLoad(
  api: ControlApi,
  number: FleetProfileNumber,
  pending: PendingProfileLoad,
): Promise<FleetProfileApplicationView> {
  // The load names the effects the operator reviewed, so a plan that changed since
  // is refused (409) instead of silently accepted. A retry sends the identical body.
  const input = {
    request_key: pending.requestKey,
    review: { effects_digest: pending.reviewedEffectsDigest },
  };
  try {
    return await api.loadProfile(number, input);
  } catch (error) {
    if (!isAmbiguousLoadFailure(error)) throw error;
    try {
      return await api.profileApplicationByRequest(number, pending.requestKey);
    } catch (lookupError) {
      if (!(lookupError instanceof ApiError && lookupError.status === 404)) throw error;
    }
    return api.loadProfile(number, input);
  }
}

const KEEP = ProfileAction.KEEP;

/** The workloads a load stops, from the reviewed effects `vonkctl run` also leads with. */
export function stoppedAliases(preview: FleetProfilePreview | undefined): string[] {
  const effects = preview?.effects;
  if (!effects) return [];
  const aliases = (effects.runs ?? []).filter((run) => run.action !== KEEP).map((r) => r.alias);
  for (const adopted of effects.adopted ?? [])
    for (const stop of adopted.stops ?? []) aliases.push(stop.effect.alias);
  return [...new Set(aliases)].sort();
}

/** The Controller's one answer for the plan: load it, load it after confirming stops, nothing to change, or blocked. */
export const loadable = (preview: FleetProfilePreview | undefined) =>
  preview?.next_action === ProfileLoadAction.LOAD ||
  preview?.next_action === ProfileLoadAction.LOAD_STOPPING;
export const nothingToChange = (preview: FleetProfilePreview | undefined) =>
  preview?.next_action === ProfileLoadAction.NOTHING_TO_CHANGE;
