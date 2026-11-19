import { useEffect, useState } from "react";
import type { ArtifactJob, ControlApi, RecipeDetail } from "../api/types";
import { safeErrorText } from "../lib/error-display";

/** What the Overview reads for a job workload: its recipe's interfaces and its run's jobs. */
export type JobWorkloadApi = Pick<ControlApi, "recipeDetail" | "artifactJobsForRun">;

const POLL_MS = 5000;

/**
 * A recipe takes jobs when it declares interfaces and none is the chat (OpenAI) one; the recipe's typed
 * `adapter` decides, never its name. A recipe that also serves chat stays a model.
 */
export function isJobRecipe(interfaces: RecipeDetail["document"]["interfaces"]): boolean {
  return interfaces.length > 0 && interfaces.every((item) => item.adapter !== "openai");
}

export type JobWorkload = {
  /** The recipe takes jobs. False while its interfaces are read, and when they cannot be read. */
  isJob: boolean;
  /** The run's jobs, newest first: undefined while they load, the message of a read that failed. */
  jobs: ArtifactJob[] | string | undefined;
};

/** Reads whether a workload's recipe takes jobs and, if so, the jobs of its run (again every few seconds). */
export function useJobWorkload(
  api: Partial<JobWorkloadApi> | undefined,
  unit: { runId: string; recipeId: string | null },
): JobWorkload {
  const { recipeDetail, artifactJobsForRun } = api ?? {};
  const [isJob, setIsJob] = useState(false);
  const [jobs, setJobs] = useState<JobWorkload["jobs"]>();
  const { runId, recipeId } = unit;
  useEffect(() => {
    setIsJob(false);
    if (!recipeDetail || !recipeId) return;
    const controller = new AbortController();
    recipeDetail(recipeId, controller.signal).then(
      (detail) => {
        if (!controller.signal.aborted) setIsJob(isJobRecipe(detail.document.interfaces));
      },
      // An unreadable recipe shows as a model: the page never blocks on it.
      () => undefined,
    );
    return () => controller.abort();
  }, [recipeDetail, recipeId]);
  useEffect(() => {
    setJobs(undefined);
    if (!isJob || !artifactJobsForRun) return;
    const controller = new AbortController();
    const read = () =>
      artifactJobsForRun(runId, controller.signal).then(
        (list) => {
          if (!controller.signal.aborted) setJobs(list.jobs);
        },
        (cause) => {
          if (!controller.signal.aborted)
            setJobs(safeErrorText(cause instanceof Error ? cause.message : "Jobs unavailable"));
        },
      );
    void read();
    const timer = window.setInterval(() => void read(), POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [isJob, artifactJobsForRun, runId]);
  return { isJob, jobs };
}
