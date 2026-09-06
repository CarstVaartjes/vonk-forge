import {useMemo} from "react";
import {formatBytes} from "../lib/fleet";

export type AvailabilityProgress = {
  phase: string;
  completedBytes: number;
  totalBytes?: number;
  bytesPerSecond?: number;
  etaSeconds?: number;
  step?: string;
  logExcerpt?: string;
};

function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

export function availabilityProgress(value: unknown): AvailabilityProgress {
  const raw = typeof value === "object" && value !== null ? value as Record<string, unknown> : {};
  const completedBytes = finiteNumber(raw.completed_bytes ?? raw.downloaded_bytes) ?? 0;
  const totalBytes = finiteNumber(raw.total_bytes ?? raw.expected_bytes);
  const bytesPerSecond = finiteNumber(raw.bytes_per_second ?? raw.rate);
  const etaSeconds = finiteNumber(raw.eta_seconds);
  const phase = typeof raw.phase === "string" && raw.phase ? raw.phase : "preparing";
  const step = typeof raw.step === "string" ? raw.step : typeof raw.current_step === "string" ? raw.current_step : undefined;
  const logExcerpt = typeof raw.log_excerpt === "string" ? raw.log_excerpt : typeof raw.log === "string" ? raw.log : undefined;
  return {phase, completedBytes, totalBytes, bytesPerSecond, etaSeconds, step, logExcerpt};
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

export function LibraryAvailabilityProgress({progress}: {progress: AvailabilityProgress}) {
  const percentage = useMemo(() => progress.totalBytes && progress.totalBytes > 0 ? Math.min(100, progress.completedBytes / progress.totalBytes * 100) : undefined, [progress.completedBytes, progress.totalBytes]);
  const transfer = progress.totalBytes === undefined
    ? `${formatBytes(progress.completedBytes)} received`
    : `${formatBytes(progress.completedBytes)} / ${formatBytes(progress.totalBytes)}`;
  return <section className="library-availability-progress" aria-label={`${phaseLabel(progress.phase)} progress`}>
    <div className="library-availability-progress-heading"><strong>{phaseLabel(progress.phase)}</strong><span>{transfer}{progress.bytesPerSecond !== undefined && ` · ${formatBytes(progress.bytesPerSecond)}/s`}{progress.etaSeconds !== undefined && ` · ${eta(progress.etaSeconds)}`}</span></div>
    <div className={`library-availability-progress-track${percentage === undefined ? " is-indeterminate" : ""}`} role="progressbar" aria-valuemin={0} aria-valuemax={progress.totalBytes ?? undefined} aria-valuenow={progress.totalBytes === undefined ? undefined : progress.completedBytes} aria-label={`${phaseLabel(progress.phase)} transfer`}>
      {percentage !== undefined && <span style={{width: `${percentage}%`}}/>}
    </div>
    {progress.step && <p>{progress.step}</p>}
    {progress.logExcerpt && <details><summary>Show latest log excerpt</summary><pre>{progress.logExcerpt}</pre></details>}
  </section>;
}
