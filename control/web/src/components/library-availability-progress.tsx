import "./library-availability-progress.css";
import {useMemo} from "react";
import {formatBytes} from "../lib/fleet";

export type AvailabilityProgress = {
  phase: string;
  completedBytes: number;
  totalBytes?: number;
  bytesPerSecond?: number;
  etaSeconds?: number;
  completedItems?: number;
  totalItems?: number;
  elapsedSeconds?: number;
  lastProgressAt?: string;
  activity?: string;
  step?: string;
  logExcerpt?: string;
};

function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

export function availabilityProgress(value: unknown): AvailabilityProgress {
  const outer = typeof value === "object" && value !== null ? value as Record<string, unknown> : {};
  const raw = typeof outer.operation === "object" && outer.operation !== null ? outer.operation as Record<string, unknown> : outer;
  const completedBytes = finiteNumber(raw.completed_bytes ?? raw.downloaded_bytes) ?? 0;
  const totalBytes = raw.total_bytes_known === false ? undefined : finiteNumber(raw.total_bytes ?? raw.expected_bytes);
  const bytesPerSecond = finiteNumber(raw.smoothed_bytes_per_second ?? raw.bytes_per_second ?? raw.rate);
  const etaSeconds = totalBytes === undefined ? undefined : finiteNumber(raw.eta_seconds);
  const phase = typeof raw.phase === "string" && raw.phase ? raw.phase : "preparing";
  const step = typeof raw.step === "string" ? raw.step : typeof raw.current_step === "string" ? raw.current_step : undefined;
  const logExcerpt = typeof raw.log_excerpt === "string" ? raw.log_excerpt : typeof raw.log === "string" ? raw.log : undefined;
  return {phase, completedBytes, totalBytes, bytesPerSecond, etaSeconds, step, logExcerpt,
    completedItems: finiteNumber(raw.completed_items), totalItems: finiteNumber(raw.total_items),
    elapsedSeconds: finiteNumber(raw.elapsed_seconds),
    lastProgressAt: typeof raw.last_progress_at === "string" ? raw.last_progress_at : undefined,
    activity: typeof raw.activity === "string" ? raw.activity : undefined,
  };
}

function phaseLabel(phase: string): string {
  return phase.replace(/[-_]+/g, " ").replace(/\b\w/g, value => value.toUpperCase());
}

function eta(value: number): string {
  if (value < 60) return `${Math.round(value)}s left`;
  const minutes = Math.floor(value / 60);
  const seconds = Math.round(value % 60);
  return seconds ? `${minutes}m ${seconds}s left` : `${minutes}m left`;
}

function observationAge(value: Date): string {
  const seconds = Math.max(0, Math.floor((Date.now() - value.getTime()) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function LibraryAvailabilityProgress({progress}: {progress: AvailabilityProgress}) {
  const transferring = /^(download(ing)?|transfer(ring)?|copying|upload(ing)?|distribution)$/.test(progress.phase);
  const percentage = useMemo(() => transferring && progress.totalBytes && progress.totalBytes > 0 ? Math.min(100, progress.completedBytes / progress.totalBytes * 100) : undefined, [progress.completedBytes, progress.totalBytes, transferring]);
  const lastProgress = progress.lastProgressAt ? new Date(progress.lastProgressAt) : undefined;
  const measured = transferring && progress.activity !== "waiting" && progress.activity !== "possibly_stalled";
  const transfer = progress.totalBytes === undefined
    ? `${formatBytes(progress.completedBytes)} received`
    : `${formatBytes(progress.completedBytes)} / ${formatBytes(progress.totalBytes)}`;
  return <section className="library-availability-progress" aria-label={`${phaseLabel(progress.phase)} progress`}>
    <div className="library-availability-progress-heading"><strong>{phaseLabel(progress.phase)}</strong><span>{transfer}{measured && progress.bytesPerSecond !== undefined && ` · ${formatBytes(progress.bytesPerSecond)}/s`}{measured && progress.etaSeconds !== undefined && ` · ${eta(progress.etaSeconds)}`}</span></div>
    <div className={`library-availability-progress-track${percentage === undefined ? " is-indeterminate" : ""}`} role="progressbar" aria-valuemin={0} aria-valuemax={percentage === undefined ? undefined : progress.totalBytes} aria-valuenow={percentage === undefined ? undefined : progress.completedBytes} aria-label={`${phaseLabel(progress.phase)} transfer`}>
      {percentage !== undefined && <span style={{width: `${percentage}%`}}/>}
    </div>
    {(progress.completedItems !== undefined || progress.activity || progress.elapsedSeconds !== undefined || lastProgress) && <p className="library-availability-progress-observation">
      {progress.activity && <span>{progress.activity === "possibly_stalled" ? "Possibly stalled · work continues" : progress.activity === "waiting" ? "Waiting for progress" : "Active"}</span>}
      {progress.completedItems !== undefined && <span>{progress.completedItems}{progress.totalItems !== undefined ? ` of ${progress.totalItems}` : ""} items</span>}
      {progress.elapsedSeconds !== undefined && <span>{Math.floor(progress.elapsedSeconds / 60)}m elapsed</span>}
      {lastProgress && !Number.isNaN(lastProgress.getTime()) && <span>Last progress <time dateTime={progress.lastProgressAt} title={lastProgress.toLocaleString()}>{observationAge(lastProgress)}</time></span>}
    </p>}
    {progress.step && <p>{progress.step}</p>}
    {progress.logExcerpt && <details><summary>Show latest log excerpt</summary><pre>{progress.logExcerpt}</pre></details>}
  </section>;
}
