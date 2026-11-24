import type { ArtifactJob, ArtifactJobFile, LibraryApi } from "../../api/types";
import { LifecycleState } from "../../api/vocabulary.generated";
import { isTerminal, jobElapsed, phaseOf } from "../../lib/artifact-job";
import { formatBytes } from "../../lib/fleet";
import { humanizeIdentifier, TechnicalDetails } from "../library/library-technical-details";
import { StatusPill } from "../ui/status-pill";
import { Time } from "../ui/time";
import { isCancelling } from "./artifact-job-submission";

function jobTone(state: string): "ok" | "warn" | "bad" | "info" {
  if (state === LifecycleState.SUCCEEDED) return "ok";
  if (state === LifecycleState.FAILED || state === LifecycleState.CANCELLED) return "bad";
  if (
    state === "running" ||
    state === "queued" ||
    state === LifecycleState.BACKOFF ||
    state === LifecycleState.OBSERVING
  )
    return "info";
  return "warn";
}

// The one owner of a job state's tone, shared with the Overview.
export { jobTone };

function OutputPreview({
  api,
  file,
  job,
}: {
  api: LibraryApi;
  file: ArtifactJobFile;
  job: ArtifactJob;
}) {
  const url = api.artifactJobResultUrl(job.id, file.name, file.sha256);
  const label = `${file.name}, ${file.media_type}, ${formatBytes(file.size_bytes)}`;
  return (
    <li className="artifact-output-row">
      <div className="artifact-output-heading">
        <div>
          <strong>{file.name}</strong>
          <span>
            {file.media_type} · {formatBytes(file.size_bytes)}
          </span>
        </div>
        <a className="button secondary" href={url} download={file.name}>
          Download
        </a>
      </div>
      {file.media_type.startsWith("image/") && (
        <img src={url} alt={`Generated output ${file.name}`} loading="lazy" />
      )}
      {file.media_type.startsWith("audio/") && (
        // biome-ignore lint/a11y/useMediaCaption: a produced file has no caption track to offer
        <audio controls preload="metadata" aria-label={`Listen to ${label}`} src={url} />
      )}
      {file.media_type.startsWith("video/") && (
        // biome-ignore lint/a11y/useMediaCaption: a produced file has no caption track to offer
        <video controls preload="metadata" aria-label={`Watch ${label}`} src={url} />
      )}
      {(file.media_type === "model/gltf-binary" || file.name.toLowerCase().endsWith(".glb")) && (
        <div className="artifact-glb-notice">
          <strong>3D artifact ready</strong>
          <span>
            This browser has no trusted GLB renderer installed. Download the exact result to inspect
            it without sending it to a third party.
          </span>
        </div>
      )}
      <TechnicalDetails compact items={[{ label: "Output SHA-256", value: file.sha256 }]} />
    </li>
  );
}

export function JobHistory({
  api,
  busyJobId,
  cancelCandidate,
  job,
  onCancel,
  onConfirmCancel,
  onPrepareRetry,
}: {
  api: LibraryApi;
  busyJobId?: string | undefined;
  cancelCandidate?: string | undefined;
  job: ArtifactJob;
  onCancel(jobId?: string): void;
  onConfirmCancel(job: ArtifactJob): void;
  onPrepareRetry(job: ArtifactJob): void;
}) {
  const active = !isTerminal(job);
  return (
    <article
      className="artifact-job-history-row"
      aria-label={`Artifact job ${job.id}, ${phaseOf(job)}`}
    >
      <header>
        <div>
          <StatusPill tone={jobTone(phaseOf(job))}>{humanizeIdentifier(phaseOf(job))}</StatusPill>
          <strong>{humanizeIdentifier(job.interface)}</strong>
          <span>
            <Time value={job.created_at} />
          </span>
        </div>
        <span>{formatBytes(job.input_total_bytes)} input</span>
      </header>
      {active && (
        <div className="artifact-job-progress" role="status" aria-live="polite">
          <span className="artifact-job-progress-track">
            <span />
          </span>
          <p>
            {job.state === "running"
              ? "The Spark is producing artifacts."
              : job.state === "queued"
                ? "Waiting for the assigned Spark."
                : "Preparing the immutable job request."}
          </p>
        </div>
      )}
      {job.status_reason && (
        <p className="artifact-job-reason" role={job.state === "failed" ? "alert" : undefined}>
          {job.status_reason}
        </p>
      )}
      {job.result_evidence && (
        <dl className="artifact-job-evidence">
          {job.result_evidence.elapsed_milliseconds != null && (
            <>
              <dt>Elapsed</dt>
              <dd>{jobElapsed(job, Date.now())}</dd>
            </>
          )}
          {job.result_evidence.peak_memory_bytes != null && (
            <>
              <dt>Peak memory</dt>
              <dd>{formatBytes(job.result_evidence.peak_memory_bytes)}</dd>
            </>
          )}
        </dl>
      )}
      {job.state === "succeeded" && job.output_files.length === 0 && (
        <p className="artifact-job-reason">This job succeeded without downloadable outputs.</p>
      )}
      {job.state === "succeeded" && job.output_files.length > 0 && (
        <ul
          className="artifact-output-list"
          aria-label={`${job.output_files.length} generated outputs`}
        >
          {job.output_files.map((file) => (
            <OutputPreview api={api} file={file} job={job} key={`${file.name}:${file.sha256}`} />
          ))}
        </ul>
      )}
      {isCancelling(job) && (
        <p className="artifact-job-notice" role="status">
          Cancellation was accepted and is waiting for the Controller to report a settled result.
        </p>
      )}
      <div className="artifact-job-row-actions">
        {active && !isCancelling(job) && cancelCandidate !== job.id && (
          <button
            type="button"
            className="button secondary"
            disabled={busyJobId === job.id}
            onClick={() => onCancel(job.id)}
          >
            Cancel job
          </button>
        )}
        {active && !isCancelling(job) && cancelCandidate === job.id && (
          <>
            <p>Cancel this job and keep its audit history?</p>
            <button
              type="button"
              className="danger"
              disabled={busyJobId === job.id}
              onClick={() => onConfirmCancel(job)}
            >
              {busyJobId === job.id ? "Cancelling…" : "Confirm cancel"}
            </button>
            <button type="button" className="button secondary" onClick={() => onCancel(undefined)}>
              Keep running
            </button>
          </>
        )}
        {(job.state === "failed" || job.state === "cancelled") && (
          <button type="button" className="button secondary" onClick={() => onPrepareRetry(job)}>
            Prepare retry
          </button>
        )}
      </div>
      <TechnicalDetails
        compact
        items={[
          { label: "Job ID", value: job.id },
          { label: "Run ID", value: job.run_id },
          { label: "Contract SHA-256", value: job.contract_sha256 },
          { label: "Input manifest", value: job.input_manifest_sha256 },
          { label: "Output manifest", value: job.output_manifest_sha256 ?? "" },
        ]}
      />
    </article>
  );
}
