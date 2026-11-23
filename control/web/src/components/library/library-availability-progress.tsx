import {
  compareWire,
  displayRatio,
  formatWire,
  isWireNumber,
  type WireNumber,
} from "../../api/contract-numeric";
import "./library-availability-progress.css";
import { useMemo } from "react";
import { BYTE_PHASES } from "../../api/vocabulary.generated";
import { formatBytes } from "../../lib/fleet";
import { phaseWords } from "../../lib/phase-words";
import { useLibraryNodeName } from "./library-node-names";
import { Time } from "../ui/time";

export type AvailabilityMember = {
  memberId: string;
  phase: string;
  completedBytes?: WireNumber | undefined;
  totalBytes?: WireNumber | undefined;
  bytesPerSecond?: number | undefined;
  etaSeconds?: number | undefined;
};

export type AvailabilityProgress = {
  phase: string;
  members: AvailabilityMember[];
  completedBytes?: WireNumber | undefined;
  totalBytes?: WireNumber | undefined;
  bytesPerSecond?: number | undefined;
  etaSeconds?: number | undefined;
  completedItems?: WireNumber | undefined;
  totalItems?: WireNumber | undefined;
  elapsedSeconds?: number | undefined;
  lastProgressAt?: string | undefined;
  activity?: string | undefined;
  step?: string | undefined;
  logExcerpt?: string | undefined;
};

function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

function nonnegativeWire(value: unknown): WireNumber | undefined {
  return isWireNumber(value) && compareWire(value, 0) >= 0 ? value : undefined;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : undefined;
}

function meter(raw: Record<string, unknown>) {
  const totalBytes = raw.total_bytes_known === false ? undefined : nonnegativeWire(raw.total_bytes);
  return {
    completedBytes: nonnegativeWire(raw.completed_bytes),
    totalBytes,
    // Rate and ETA are the Controller's own figures; nothing is derived here.
    bytesPerSecond: finiteNumber(raw.smoothed_bytes_per_second),
    etaSeconds: totalBytes === undefined ? undefined : finiteNumber(raw.eta_seconds),
  };
}

function memberProgress(value: unknown, phase: string): AvailabilityMember[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const raw = asRecord(item);
    if (!raw || typeof raw.member_id !== "string") return [];
    return [
      {
        memberId: raw.member_id,
        phase: typeof raw.phase === "string" && raw.phase ? raw.phase : phase,
        ...meter(raw),
      },
    ];
  });
}

export function availabilityProgress(value: unknown): AvailabilityProgress {
  const outer = asRecord(value) ?? {};
  const raw = asRecord(outer.operation) ?? outer;
  const phase = typeof raw.phase === "string" && raw.phase ? raw.phase : "preparing";
  const step =
    typeof raw.step === "string"
      ? raw.step
      : typeof raw.current_step === "string"
        ? raw.current_step
        : undefined;
  const logExcerpt =
    typeof raw.log_excerpt === "string"
      ? raw.log_excerpt
      : typeof raw.log === "string"
        ? raw.log
        : undefined;
  return {
    phase,
    members: memberProgress(raw.members, phase),
    ...meter(raw),
    step,
    logExcerpt,
    completedItems: nonnegativeWire(raw.completed_items),
    totalItems: nonnegativeWire(raw.total_items),
    elapsedSeconds: finiteNumber(raw.elapsed_seconds),
    lastProgressAt: typeof raw.last_progress_at === "string" ? raw.last_progress_at : undefined,
    activity: typeof raw.activity === "string" ? raw.activity : undefined,
  };
}

function eta(value: number): string {
  if (value < 60) return `${Math.round(value)}s left`;
  const minutes = Math.floor(value / 60);
  const seconds = Math.round(value % 60);
  return seconds ? `${minutes}m ${seconds}s left` : `${minutes}m left`;
}

/** One Spark's own bar: bytes, percent, and the Controller's rate and ETA when given. */
function MemberBar({ member }: { member: AvailabilityMember }) {
  const memberName = useLibraryNodeName();
  const percent =
    member.completedBytes !== undefined &&
    member.totalBytes !== undefined &&
    compareWire(member.totalBytes, 0) > 0
      ? Math.min(100, displayRatio(member.completedBytes, member.totalBytes) * 100)
      : undefined;
  const sizes =
    member.completedBytes === undefined
      ? undefined
      : member.totalBytes === undefined
        ? `${formatBytes(member.completedBytes)} received`
        : `${formatBytes(member.completedBytes)} / ${formatBytes(member.totalBytes)}`;
  return (
    <li className="library-availability-progress-member">
      <div className="library-availability-progress-heading">
        <strong>{memberName(member.memberId)}</strong>
        <span>
          {sizes}
          {percent !== undefined && ` · ${Math.floor(percent)}%`}
          {member.bytesPerSecond !== undefined && ` · ${formatBytes(member.bytesPerSecond)}/s`}
          {member.etaSeconds !== undefined && (
            <span className="library-availability-progress-eta"> · {eta(member.etaSeconds)}</span>
          )}
        </span>
      </div>
      <div
        className={`library-availability-progress-track${percent === undefined ? " is-indeterminate" : ""}`}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={percent === undefined ? undefined : 100}
        aria-valuenow={percent}
        aria-label={`${memberName(member.memberId)} ${phaseWords(member.phase)}`}
      >
        {percent !== undefined && <span style={{ width: `${percent}%` }} />}
      </div>
    </li>
  );
}

export function LibraryAvailabilityProgress({ progress }: { progress: AvailabilityProgress }) {
  const transferring = BYTE_PHASES.has(progress.phase);
  const percentage = useMemo(
    () =>
      transferring &&
      progress.completedBytes !== undefined &&
      progress.totalBytes !== undefined &&
      compareWire(progress.totalBytes, 0) > 0
        ? Math.min(100, displayRatio(progress.completedBytes, progress.totalBytes) * 100)
        : undefined,
    [progress.completedBytes, progress.totalBytes, transferring],
  );
  const lastProgress = progress.lastProgressAt ? new Date(progress.lastProgressAt) : undefined;
  const measured =
    transferring && progress.activity !== "waiting" && progress.activity !== "possibly_stalled";
  const transfer =
    progress.completedBytes === undefined
      ? undefined
      : progress.totalBytes === undefined
        ? `${formatBytes(progress.completedBytes)} received`
        : `${formatBytes(progress.completedBytes)} / ${formatBytes(progress.totalBytes)}`;
  return (
    <section
      className="library-availability-progress"
      aria-label={`${phaseWords(progress.phase)} progress`}
    >
      <div className="library-availability-progress-heading">
        <strong>{phaseWords(progress.phase)}</strong>
        <span>
          {transfer}
          {percentage !== undefined && ` · ${Math.floor(percentage)}%`}
          {measured &&
            progress.bytesPerSecond !== undefined &&
            ` · ${formatBytes(progress.bytesPerSecond)}/s`}
          {measured && progress.etaSeconds !== undefined && (
            <span className="library-availability-progress-eta"> · {eta(progress.etaSeconds)}</span>
          )}
        </span>
      </div>
      <div
        className={`library-availability-progress-track${percentage === undefined ? " is-indeterminate" : ""}`}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={percentage === undefined ? undefined : 100}
        aria-valuenow={percentage}
        aria-valuetext={transfer}
        aria-label={`${phaseWords(progress.phase)} transfer`}
      >
        {percentage !== undefined && <span style={{ width: `${percentage}%` }} />}
      </div>
      {progress.members.length > 1 && (
        <ul className="library-availability-progress-members" aria-label="Progress per Spark">
          {progress.members.map((member) => (
            <MemberBar key={member.memberId} member={member} />
          ))}
        </ul>
      )}
      {(progress.completedItems !== undefined ||
        progress.activity ||
        progress.elapsedSeconds !== undefined ||
        lastProgress) && (
        <p className="library-availability-progress-observation">
          {progress.activity && (
            <span>
              {progress.activity === "possibly_stalled"
                ? "Possibly stalled · work continues"
                : progress.activity === "waiting"
                  ? "Waiting for progress"
                  : "Active"}
            </span>
          )}
          {progress.completedItems !== undefined && (
            <span>
              {formatWire(progress.completedItems)}
              {progress.totalItems !== undefined ? ` of ${formatWire(progress.totalItems)}` : ""}{" "}
              items
            </span>
          )}
          {progress.elapsedSeconds !== undefined && (
            <span>{Math.floor(progress.elapsedSeconds / 60)}m elapsed</span>
          )}
          {lastProgress && progress.lastProgressAt && !Number.isNaN(lastProgress.getTime()) && (
            <span>
              Last progress <Time value={progress.lastProgressAt} />
            </span>
          )}
        </p>
      )}
      {progress.step && <p>{progress.step}</p>}
      {progress.logExcerpt && (
        <details>
          <summary>Show latest log excerpt</summary>
          <pre>{progress.logExcerpt}</pre>
        </details>
      )}
    </section>
  );
}
