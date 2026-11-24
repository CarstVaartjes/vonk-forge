import { useId, useState } from "react";
import { formatWire, type WireNumber } from "../api/contract-numeric";
import { exactTime, relativeTime } from "../lib/time";
import { type ActivityRecord, targetSummaryNames } from "./activity-records";

export function EventTime({ event, now }: { event: ActivityRecord; now: Date }) {
  if (!event.occurred_at)
    return (
      <span className="activity-time is-unavailable">
        <strong>Time not recorded</strong>
        <small>The current record has no timestamp</small>
      </span>
    );
  const exact = exactTime(event.occurred_at);
  const relative = relativeTime(event.occurred_at, now);
  if (!exact || !relative)
    return (
      <span className="activity-time is-unavailable">
        <strong>Time not recorded</strong>
        <small>The recorded timestamp is invalid</small>
      </span>
    );
  return (
    <time className="activity-time" dateTime={event.occurred_at} title={exact}>
      <strong>{relative}</strong>
      <small>{exact}</small>
    </time>
  );
}

export function CopyableValue({
  label,
  value,
}: {
  label: string;
  value?: string | null | undefined;
}) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const statusId = useId();
  if (!value)
    return (
      <div>
        <dt>{label}</dt>
        <dd>Not recorded</dd>
      </div>
    );
  async function copy(): Promise<void> {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(value!);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }
  return (
    <div>
      <dt>{label}</dt>
      <dd className="activity-copy-value">
        <code>{value}</code>
        <button
          type="button"
          className="activity-copy"
          aria-describedby={statusId}
          onClick={() => void copy()}
          aria-label={`Copy ${label.toLowerCase()}`}
        >
          {copyState === "copied" ? "Copied" : "Copy"}
        </button>
      </dd>
      {copyState === "failed" && (
        <dd className="activity-copy-error">
          Clipboard access is unavailable. Select the value to copy it.
        </dd>
      )}
      <dd className="sr-only" id={statusId} aria-live="polite">
        {copyState === "copied"
          ? `${label} copied`
          : copyState === "failed"
            ? `Could not copy ${label.toLocaleLowerCase()}`
            : ""}
      </dd>
    </div>
  );
}

export function DiagnosticDownload({ id, attempt }: { id: string; attempt: WireNumber }) {
  return (
    <a
      className="button secondary"
      href={`/api/operations/${encodeURIComponent(id)}/evidence?attempt=${encodeURIComponent(formatWire(attempt))}`}
      download
    >
      Download diagnostics
    </a>
  );
}

export function TechnicalDetails({ event }: { event: ActivityRecord }) {
  return (
    <details className="activity-technical">
      <summary>Technical details</summary>
      <dl>
        <CopyableValue
          label={event.source === "operation" ? "Operation ID" : "Request ID"}
          value={event.request_id}
        />
        {event.targets.length === 0 ? (
          <div>
            <dt>Targets</dt>
            <dd>None recorded</dd>
          </div>
        ) : (
          event.targets.map((target, index) => (
            <CopyableValue
              // biome-ignore lint/suspicious/noArrayIndexKey: the same target can repeat; the position tells the copies apart
              key={`${target}:${index}`}
              label={event.targets.length === 1 ? "Target" : `Target ${index + 1}`}
              value={target}
            />
          ))
        )}
      </dl>
    </details>
  );
}

export function TargetSummary({
  compact = false,
  event,
}: {
  compact?: boolean;
  event: ActivityRecord;
}) {
  if (event.targets.length === 0) return null;
  const names = targetSummaryNames(event);
  const content = (
    <>
      <span>{names.length === 1 ? "Target" : "Targets"}</span> <strong>{names.join(" · ")}</strong>
    </>
  );
  return compact ? (
    <small className="activity-target-summary">{content}</small>
  ) : (
    <span className="activity-target-summary">{content}</span>
  );
}
