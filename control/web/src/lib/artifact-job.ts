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
