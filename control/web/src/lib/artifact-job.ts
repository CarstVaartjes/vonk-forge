import type { ArtifactJob, ArtifactJobInterface, RecipeDefinition } from "../api/types";
import { LifecycleState } from "../api/vocabulary.generated";

/** The job interfaces of a recipe: everything it declares that is not the chat (OpenAI) one. */
export type JobRecipeInterface = Extract<
  RecipeDefinition["interfaces"][number],
  { adapter: ArtifactJobInterface }
>;

export function jobInterfacesOf(
  document: Pick<RecipeDefinition, "interfaces">,
): JobRecipeInterface[] {
  return document.interfaces.filter(
    (item): item is JobRecipeInterface => item.adapter !== "openai",
  );
}

const TERMINAL_STATES = new Set<string>([
  LifecycleState.SUCCEEDED,
  LifecycleState.FAILED,
  LifecycleState.CANCELLED,
]);

/** The name a job is shown under: its lifecycle state, or its preparation stage before it is submitted. */
export function phaseOf(job: ArtifactJob): string {
  return job.state ?? job.preparation ?? "draft";
}

export function isTerminal(job: ArtifactJob): boolean {
  return job.state != null && TERMINAL_STATES.has(job.state);
}

export function jobTone(state: string): "ok" | "warn" | "bad" | "info" {
  if (state === LifecycleState.SUCCEEDED) return "ok";
  if (state === LifecycleState.FAILED || state === LifecycleState.CANCELLED) return "bad";
  if (
    state === LifecycleState.RUNNING ||
    state === LifecycleState.QUEUED ||
    state === LifecycleState.BACKOFF ||
    state === LifecycleState.OBSERVING
  )
    return "info";
  return "warn";
}

/** The action a job interface offers: "Create a video". */
export function createJobTitle(adapter: ArtifactJobInterface): string {
  const noun: Record<ArtifactJobInterface, string> = {
    "audio-job": "audio",
    "video-job": "a video",
    "image-job": "an image",
    "mesh-job": "a 3D model",
    "artifact-job": "an artifact",
  };
  return `Create ${noun[adapter]}`;
}

/** Minutes and seconds ("15m 16s"), or seconds alone under a minute. */
export function elapsedText(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return whole < 60 ? `${whole}s` : `${Math.floor(whole / 60)}m ${whole % 60}s`;
}

/**
 * How long a job took or has taken: a settled job uses the result's own elapsed time when it has one,
 * else its last change minus its start; any other job runs from its start until `now`.
 */
export function jobElapsed(job: ArtifactJob, now: number): string {
  const start = Date.parse(job.created_at);
  if (!isTerminal(job)) return Number.isNaN(start) ? "" : elapsedText((now - start) / 1000);
  const measured = job.result_evidence?.elapsed_milliseconds;
  if (measured != null) return elapsedText(Number(measured) / 1000);
  const end = Date.parse(job.updated_at);
  return Number.isNaN(start) || Number.isNaN(end) ? "" : elapsedText((end - start) / 1000);
}
