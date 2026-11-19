import type { ArtifactJob, ArtifactJobInterface } from "../api/types";
import { createJobTitle, jobElapsed, phaseOf } from "../lib/artifact-job";
import { jobTone } from "./artifact-job-workspace";
import { recipeLibraryPath } from "../lib/library-route";
import { humanizeIdentifier } from "./library-technical-details";
import { StatusPill } from "./status-pill";

/**
 * What a job workload shows in place of engine figures: one line that engine metrics do not apply, the
 * way to create a job, and its latest jobs (state, step, elapsed) with a link to the recipe page, where
 * the job form and results are.
 */
export function WorkloadJobs({
  revision,
  adapter,
  jobs,
}: {
  /** The running recipe revision: its page opens that exact revision. */
  revision: string;
  adapter: ArtifactJobInterface;
  jobs: ArtifactJob[] | string | undefined;
}) {
  const now = Date.now();
  const page = recipeLibraryPath({ revision });
  return (
    <section className="workload-jobs" aria-label="Jobs">
      <p className="hero-caption">
        This recipe takes jobs, not chat requests, so engine metrics do not apply.
      </p>
      <p>
        <a className="button" href={page}>
          {createJobTitle(adapter)}
        </a>
      </p>
      {typeof jobs === "string" && <p role="status">Jobs unavailable: {jobs}</p>}
      {Array.isArray(jobs) && jobs.length === 0 && <p>No jobs yet.</p>}
      {Array.isArray(jobs) && jobs.length > 0 && (
        <ul className="workload-job-list">
          {jobs.map((job) => (
            <li key={job.id}>
              <StatusPill tone={jobTone(phaseOf(job))}>
                {humanizeIdentifier(phaseOf(job))}
              </StatusPill>{" "}
              <strong>{humanizeIdentifier(job.interface)}</strong>
              {" · "}
              {jobElapsed(job, now)}
              {job.status_reason ? ` · ${job.status_reason}` : ""}
              {" · "}
              <a href={page}>Open</a>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** A workload whose recipe has not been read yet: nothing is claimed about it. */
export function WorkloadChecking({ error }: { error?: string }) {
  return (
    <p className="hero-caption" role="status">
      {error ? `Recipe unavailable: ${error}. Retrying.` : "Reading what this recipe serves…"}
    </p>
  );
}
