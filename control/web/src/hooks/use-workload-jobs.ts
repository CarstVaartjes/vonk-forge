import { useEffect, useState } from "react";
import { type ArtifactJob, type ArtifactJobInterface, type ControlApi } from "../api/types";
import { jobInterfacesOf } from "../lib/artifact-job";
import { safeErrorText } from "../lib/error-display";

/** What the Overview reads for a workload: its recipe revision, and the jobs of its run. */
export type JobWorkloadApi = Pick<ControlApi, "recipeDetail" | "artifactJobsForRun">;

const POLL_MS = 5000;
const RECENT = 5;

/**
 * What a workload serves: unread until its recipe has been read (it is read again every few seconds until
 * it is, and `error` says why not), chat for a model recipe, jobs for a recipe that takes jobs.
 */
export const WorkloadKind = { UNREAD: "unread", CHAT: "chat", JOBS: "jobs" } as const;

export type JobWorkload =
  | { kind: typeof WorkloadKind.UNREAD; error?: string }
  | { kind: typeof WorkloadKind.CHAT }
  | {
      kind: typeof WorkloadKind.JOBS;
      /** The running recipe revision, for its page. */
      revision: string;
      interface: ArtifactJobInterface;
      /** The run's latest jobs: undefined while they load, the message of a read that failed. */
      jobs: ArtifactJob[] | string | undefined;
    };

function canRead(api: Partial<JobWorkloadApi> | undefined): api is JobWorkloadApi {
  return Boolean(api?.recipeDetail && api.artifactJobsForRun);
}

/**
 * A recipe takes jobs when it declares interfaces and none is the chat one; the typed interface decides,
 * never a name. Without `api` (or a revision to read) the workload is a model.
 */
export function useJobWorkload(
  api: Partial<JobWorkloadApi> | undefined,
  unit: { runId: string; recipeRevisionId: string | null },
): JobWorkload {
  const { runId, recipeRevisionId } = unit;
  const [state, setState] = useState<JobWorkload>({ kind: WorkloadKind.UNREAD });
  useEffect(() => {
    setState({ kind: WorkloadKind.UNREAD });
    if (!canRead(api) || !recipeRevisionId) return;
    const reads = api;
    const controller = new AbortController();
    let busy = false;
    let job: { interface: ArtifactJobInterface } | undefined;
    const tick = async () => {
      if (busy) return;
      busy = true;
      try {
        if (!job) {
          const detail = await reads.recipeDetail(recipeRevisionId, controller.signal);
          const jobs = jobInterfacesOf(detail.document);
          const [firstJob] = jobs;
          if (!firstJob || jobs.length < detail.document.interfaces.length) {
            window.clearInterval(timer);
            if (!controller.signal.aborted) setState({ kind: WorkloadKind.CHAT });
            return;
          }
          job = { interface: firstJob.adapter };
        }
        let jobs: ArtifactJob[] | string;
        try {
          jobs = (await reads.artifactJobsForRun(runId, controller.signal, RECENT)).jobs;
        } catch (cause) {
          if (controller.signal.aborted) return;
          jobs = safeErrorText(cause instanceof Error ? cause.message : "Jobs unavailable");
        }
        if (!controller.signal.aborted)
          setState({ kind: WorkloadKind.JOBS, revision: recipeRevisionId, ...job, jobs });
      } catch (cause) {
        if (!controller.signal.aborted)
          setState({
            kind: WorkloadKind.UNREAD,
            error: safeErrorText(cause instanceof Error ? cause.message : "Recipe unavailable"),
          });
      } finally {
        busy = false;
      }
    };
    const timer = window.setInterval(() => void tick(), POLL_MS);
    void tick();
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [api, runId, recipeRevisionId]);
  // Without a way to read the recipe the workload shows as a model, as before.
  if (!canRead(api) || !recipeRevisionId) return { kind: WorkloadKind.CHAT };
  return state;
}
