import type { ArtifactJob } from "../api/types";
import { recipeLibraryPath } from "../lib/library-route";
import { humanizeIdentifier } from "./library-technical-details";
import { StatusPill } from "./status-pill";

const RECENT = 5;
const ACTIVE = new Set(["queued", "running", "backoff", "observing"]);

/** Minutes and seconds ("15m 16s"), or seconds alone under a minute. */
export function elapsedText(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return whole < 60 ? `${whole}s` : `${Math.floor(whole / 60)}m ${whole % 60}s`;
}

function tone(state: string): "ok" | "warn" | "bad" | "info" {
  if (state === "succeeded") return "ok";
  if (state === "failed" || state === "cancelled") return "bad";
  return ACTIVE.has(state) ? "info" : "warn";
}

/** A job's age: running so far while it is active, start to last change once it has settled. */
function elapsed(job: ArtifactJob, now: number): string {
  const start = Date.parse(job.created_at);
  const end = ACTIVE.has(job.state ?? "") ? now : Date.parse(job.updated_at);
  return Number.isNaN(start) || Number.isNaN(end) ? "" : elapsedText((end - start) / 1000);
}

/**
 * What a job workload shows in place of engine figures: one line that engine metrics do not apply, its
 * recent jobs (state, step, elapsed) and the way to the recipe page, where the job form and results are.
 */
export function WorkloadJobs({
  recipeId,
  jobs,
}: {
  recipeId: string | null;
  jobs: ArtifactJob[] | string | undefined;
}) {
  const now = Date.now();
  const recent = Array.isArray(jobs)
    ? [...jobs].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at)).slice(0, RECENT)
    : [];
  const page = recipeId ? recipeLibraryPath(recipeId) : undefined;
  return (
    <section className="workload-jobs" aria-label="Jobs">
      <p className="hero-caption">
        This recipe takes jobs, not chat requests, so engine metrics do not apply.
      </p>
      {page && (
        <p>
          <a className="button" href={page}>
            Create job
          </a>
        </p>
      )}
      {typeof jobs === "string" && <p role="status">Jobs unavailable: {jobs}</p>}
      {Array.isArray(jobs) && recent.length === 0 && <p>No jobs yet.</p>}
      {recent.length > 0 && (
        <ul className="workload-job-list">
          {recent.map((job) => {
            const state = job.state ?? job.preparation ?? "draft";
            return (
              <li key={job.id}>
                <StatusPill tone={tone(state)}>{humanizeIdentifier(state)}</StatusPill>{" "}
                <strong>{humanizeIdentifier(job.interface)}</strong>
                {" · "}
                {elapsed(job, now)}
                {job.status_reason ? ` · ${job.status_reason}` : ""}
                {page && (
                  <>
                    {" · "}
                    <a href={page}>Open</a>
                  </>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
