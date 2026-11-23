import {
  addWire,
  compareWire,
  displayRatio,
  formatWire,
  type WireNumber,
} from "../api/contract-numeric";
import { ApiError } from "../api/errors";
import {
  availabilityProgress,
  LibraryAvailabilityProgress,
} from "../components/library/library-availability-progress";
import {
  availabilityFailure,
  LibraryAvailabilityFeedback,
} from "../components/library/library-availability-feedback";
import {
  LIVE_LIFECYCLE_STATES,
  LifecycleState,
  TERMINAL_LIFECYCLE_STATES,
  UNSUCCESSFUL_LIFECYCLE_STATES,
} from "../api/vocabulary.generated";

/** A wait for a person. */
export function isOperatorWait(state: string | undefined): boolean {
  return state === LifecycleState.NEEDS_OPERATOR;
}
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { SyntheticEvent } from "react";
import type {
  ActivityFilters,
  ControlApi,
  JobDetail,
  OperationDetail,
  VisualFleetSnapshot,
} from "../api/types";
import { EmptyState } from "../components/ui/empty-state";
import { PageHeader } from "../components/ui/page-header";
import { SkeletonBlock, SkeletonRows } from "../components/ui/skeleton";
import { StatusPill } from "../components/ui/status-pill";
import { NOT_REPORTED_TEXT, nodeDisplayName } from "../lib/fleet";
import { exactTime, relativeTime } from "../lib/time";

export { relativeTime };
type ActivityView = "timeline" | "table";
type ActivityStatus =
  | "recorded"
  | "in_progress"
  | "attention"
  | "unsuccessful"
  | "called_off"
  | "unknown";
type ActivitySummary = { request_id: string; actor: string; action: string; targets: string[] };
type ActivityRecord = ActivitySummary & {
  occurred_at?: string | null | undefined;
  source: "job" | "operation";
  target_names?: string[];
  operation?: OperationDetail;
  /** The Controller's plain phrase for the event, and the workload it acts on. */
  summary?: string;
  subject?: string | null | undefined;
};
type ActivityApi = Pick<
  ControlApi,
  "job" | "resumeJob" | "visualFleet" | "operations" | "operation"
>;

const VIEW_PREFERENCE_KEY = "vonk.activity.view";

function failureSummary(failure: OperationDetail["failure"]): string {
  if (!failure) return "";
  if ("code" in failure) return failure.detail;
  if ("reason" in failure) return failure.summary ?? failure.reason ?? failure.error_code ?? "";
  return failure.summary ?? failure.error_code ?? "";
}

function failureText(failure: OperationDetail["failure"]): string {
  if (!failure) return "";
  return [
    failureSummary(failure),
    "detail" in failure ? failure.detail : "",
    "code" in failure ? failure.code : failure.error_code,
  ]
    .filter(Boolean)
    .join(" ");
}

const CATEGORY_LABELS: Record<string, string> = {
  operation: "Operations",
};

function titleCase(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function activityCategory(event: ActivitySummary): string {
  const category = event.action.split(".")[0] || "other";
  return CATEGORY_LABELS[category] ?? titleCase(category);
}

/** The lifecycle words, grouped by the contract's sets: a cancel is the operator's own request, not a failure. */
function lifecycleStatus(state: string): ActivityStatus | undefined {
  if (UNSUCCESSFUL_LIFECYCLE_STATES.has(state)) return "unsuccessful";
  if (state === LifecycleState.CANCELLED) return "called_off";
  if (state === LifecycleState.NEEDS_OPERATOR) return "attention";
  if (TERMINAL_LIFECYCLE_STATES.has(state)) return "recorded";
  if (LIVE_LIFECYCLE_STATES.has(state)) return "in_progress";
  return undefined;
}

/** The states of other operation kinds that are not lifecycle words. */
const OPERATION_STATUS: Record<string, ActivityStatus> = {
  canceled: "called_off",
  error: "unsuccessful",
  expired: "unsuccessful",
  uncertain: "attention",
  waiting: "in_progress",
  compensating: "in_progress",
  pending: "in_progress",
  planned: "in_progress",
  starting: "in_progress",
  stopping: "in_progress",
  compensated: "recorded",
  completed: "recorded",
};

/** An operation state as the Activity page classes it; a state not listed here is not known. */
function operationStatus(state: string): ActivityStatus {
  return lifecycleStatus(state) ?? OPERATION_STATUS[state] ?? "unknown";
}

/** Finished work does not change again; every other state (waiting, uncertain, not known) can. */
export function operationEnded(state: string): boolean {
  const status = operationStatus(state);
  return status === "unsuccessful" || status === "called_off" || status === "recorded";
}

export function activityStatus(event: ActivitySummary): ActivityStatus {
  if (event.action.startsWith("operation."))
    return operationStatus(event.action.split(".").at(-1) ?? "");
  if (/(?:^|\.)(?:failed|rejected|throttled|denied|error)(?:\.|$)/.test(event.action))
    return "unsuccessful";
  if (/(?:^|\.)(?:uncertain|warning|conflict|stale)(?:\.|$)/.test(event.action)) return "attention";
  return "recorded";
}

function statusLabel(status: ActivityStatus): string {
  if (status === "unsuccessful") return "Unsuccessful";
  if (status === "called_off") return "Cancelled";
  if (status === "attention") return "Needs review";
  if (status === "in_progress") return "In progress";
  if (status === "unknown") return "Unknown state";
  return "Recorded";
}

function statusTone(status: ActivityStatus): "neutral" | "ok" | "warn" | "bad" | "info" {
  if (status === "unsuccessful") return "bad";
  if (status === "attention") return "warn";
  if (status === "in_progress") return "info";
  if (status === "unknown" || status === "called_off") return "neutral";
  return "ok";
}

/** The statuses a link can open the page on. */
const LINKABLE_STATUSES: readonly ActivityStatus[] = [
  "recorded",
  "in_progress",
  "attention",
  "unsuccessful",
  "called_off",
];

const OPERATION_STATES = [
  "queued",
  "running",
  LifecycleState.BACKOFF,
  LifecycleState.OBSERVING,
  "waiting",
  LifecycleState.NEEDS_OPERATOR,
  "compensating",
  "uncertain",
  "failed",
  "cancelled",
  "superseded",
  "succeeded",
];
const REQUEST_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

/** Filters live in the URL so a filtered view can be linked and reloaded, like `vonkctl fleet activity --state --target --request-id`. */
function readFilters(): ActivityFilters {
  const params = new URLSearchParams(location.search);
  return {
    state: params.get("state") || undefined,
    target: params.get("target") || undefined,
    requestId: params.get("request_id") || undefined,
  };
}

/** `?status=unsuccessful` opens the page already filtered, so other pages can link a count to its events. */
function readStatus(): ActivityStatus | "" {
  const value = new URLSearchParams(location.search).get("status");
  return LINKABLE_STATUSES.find((status) => status === value) ?? "";
}

function writeFilters(filters: ActivityFilters): void {
  const params = new URLSearchParams(location.search);
  for (const [key, value] of [
    ["state", filters.state],
    ["target", filters.target],
    ["request_id", filters.requestId],
  ] as const) {
    if (value) params.set(key, value);
    else params.delete(key);
  }
  const query = params.toString();
  history.replaceState(history.state, "", `${location.pathname}${query ? `?${query}` : ""}`);
}

function readViewPreference(): ActivityView {
  try {
    return localStorage.getItem(VIEW_PREFERENCE_KEY) === "table" ? "table" : "timeline";
  } catch {
    return "timeline";
  }
}

function EventTime({ event, now }: { event: ActivityRecord; now: Date }) {
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

function CopyableValue({ label, value }: { label: string; value?: string | null | undefined }) {
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

function DiagnosticDownload({ id, attempt }: { id: string; attempt: WireNumber }) {
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

function TechnicalDetails({ event }: { event: ActivityRecord }) {
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

function unavailableTargetLabel(target: string): string {
  return target.startsWith("spk_")
    ? "Spark no longer registered"
    : "Target not in current inventory";
}

function targetSummaryNames(event: ActivityRecord): string[] {
  const names: string[] = [];
  let historicalSparks = 0;
  let historicalTargets = 0;
  event.targets.forEach((target, index) => {
    const name = event.target_names?.[index];
    if (name) names.push(name);
    else if (target.startsWith("spk_")) historicalSparks += 1;
    else historicalTargets += 1;
  });
  if (historicalSparks > 0)
    names.push(`${historicalSparks} historical ${historicalSparks === 1 ? "Spark" : "Sparks"}`);
  if (historicalTargets > 0)
    names.push(`${historicalTargets} historical ${historicalTargets === 1 ? "target" : "targets"}`);
  return names;
}

/** The entry's headline: the workload (else the Sparks) it concerns, then what happened, in the Controller's words. */
export function activityTitle(event: ActivityRecord): string {
  const names = targetSummaryNames(event);
  const sparks =
    names.length > 2 ? `${names.slice(0, 2).join(", ")} +${names.length - 2}` : names.join(", ");
  const subject = event.subject || sparks;
  const summary = event.summary || "Recorded activity";
  return subject ? `${subject} · ${summary}` : summary;
}

function TargetSummary({ compact = false, event }: { compact?: boolean; event: ActivityRecord }) {
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

const LIVE_JOB_STATES = new Set<string>([
  LifecycleState.BACKOFF,
  LifecycleState.OBSERVING,
  "compensating",
  "pending",
  "planned",
  "queued",
  "running",
  "starting",
  "stopping",
  "waiting",
]);

/** The one decision "this card polls itself": live work needs no Refresh button. */
function stateUpdatesItself(state: string): boolean {
  return LIVE_JOB_STATES.has(state);
}

function jobUpdatesAutomatically(detail: JobDetail): boolean {
  return (
    stateUpdatesItself(detail.state) ||
    (isOperatorWait(detail.state) &&
      (detail.agent_upgrade_diagnostics?.targets.some((target) => target.retry_queued) ?? false))
  );
}

function jobRefreshIntervalMs(detail: JobDetail): number {
  return detail.state === "waiting" || detail.state === LifecycleState.OBSERVING ? 60_000 : 5_000;
}

function friendlyTarget(target: string, names: Map<string, string>): string {
  return names.get(target) || unavailableTargetLabel(target);
}

function AgentUpgradeDiagnostics({
  detail,
  targetNames,
}: {
  detail: JobDetail;
  targetNames: Map<string, string>;
}) {
  const diagnostics = detail.agent_upgrade_diagnostics;
  if (!diagnostics) return null;
  const expected = diagnostics.expected_identity;
  return (
    <section className="activity-upgrade-diagnostics" aria-label="Agent upgrade diagnosis">
      <header>
        <div>
          <span>Expected release</span>
          <strong>{expected.version || "Not recorded"}</strong>
        </div>
        <StatusPill
          tone={diagnostics.targets.every((target) => target.target_proven) ? "ok" : "warn"}
        >
          {diagnostics.targets.every((target) => target.target_proven)
            ? "Identity proven"
            : "Identity not proven"}
        </StatusPill>
      </header>
      <dl className="activity-upgrade-expected">
        <CopyableValue label="Target binary digest" value={expected.binary_digest} />
        <CopyableValue label="Target build digest" value={expected.build_digest} />
      </dl>
      <ul>
        {diagnostics.targets.map((target) => (
          <li key={target.node_id}>
            <div className="activity-upgrade-target">
              <strong>{friendlyTarget(target.node_id, targetNames)}</strong>
              <span>
                {formatWire(target.attempts)} install{" "}
                {compareWire(target.attempts, 1) === 0 ? "attempt" : "attempts"} ·{" "}
                {target.target_proven ? "exact target reported" : "exact target not reported"}
              </span>
            </div>
            <dl>
              <div>
                <dt>Observed version</dt>
                <dd>{target.observed_identity.version || NOT_REPORTED_TEXT}</dd>
              </div>
              <CopyableValue
                label="Observed binary digest"
                value={target.observed_identity.binary_digest}
              />
              <CopyableValue
                label="Observed build digest"
                value={target.observed_identity.build_digest}
              />
              {target.retry_not_before && (
                <div>
                  <dt>
                    {target.retry_queued ? "Controller retry not before" : "Retry not before"}
                  </dt>
                  <dd>
                    <time dateTime={target.retry_not_before}>
                      {exactTime(target.retry_not_before) || target.retry_not_before}
                    </time>
                  </dd>
                </div>
              )}
            </dl>
            {target.raw_reason && (
              <details>
                <summary>Raw helper evidence</summary>
                <code>{target.raw_reason}</code>
              </details>
            )}
          </li>
        ))}
      </ul>
      {diagnostics.failure_details_unavailable && (
        <p className="activity-upgrade-ambiguity">
          <strong>Helper did not report the failed stage.</strong> It does not prove that
          authorization or download failed, and it does not prove that the package installed. The
          exact runtime identity remains the success gate.
        </p>
      )}
    </section>
  );
}

function JobProgressDetails({
  api,
  event,
  onUpdate,
  targetNames,
}: {
  api: Pick<ActivityApi, "job" | "resumeJob">;
  event: ActivityRecord;
  onUpdate: (detail: JobDetail) => void;
  targetNames: Map<string, string>;
}) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<JobDetail>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [resumeError, setResumeError] = useState("");
  const [resumeNotice, setResumeNotice] = useState("");
  const [resuming, setResuming] = useState(false);
  const loadingRef = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const loadDetail = useCallback(
    async (background = false): Promise<JobDetail | undefined> => {
      if (loadingRef.current) return undefined;
      loadingRef.current = true;
      setLoading(true);
      if (!background) setError("");
      try {
        const next = await api.job(event.request_id);
        if (!mountedRef.current) return undefined;
        setDetail(next);
        setError("");
        onUpdate(next);
        return next;
      } catch (value) {
        if (mountedRef.current)
          setError(
            value instanceof Error ? value.message : "Unable to load current operation details.",
          );
        return undefined;
      } finally {
        loadingRef.current = false;
        if (mountedRef.current) setLoading(false);
      }
    },
    [api, event.request_id, onUpdate],
  );

  useEffect(() => {
    if (!open || !detail || !jobUpdatesAutomatically(detail)) return undefined;
    const interval = window.setInterval(() => loadDetail(true), jobRefreshIntervalMs(detail));
    return () => window.clearInterval(interval);
  }, [detail, loadDetail, open]);

  function toggle(event: SyntheticEvent<HTMLDetailsElement>): void {
    const nextOpen = event.currentTarget.open;
    setOpen(nextOpen);
    if (nextOpen && !detail && !loadingRef.current) void loadDetail();
  }

  async function resume(): Promise<void> {
    if (!detail || !isOperatorWait(detail.state) || resuming) return;
    setResuming(true);
    setResumeError("");
    setResumeNotice("");
    try {
      const response = await api.resumeJob(event.request_id);
      if (!mountedRef.current) return;
      const resumed = { ...detail, state: response.state };
      setDetail(resumed);
      onUpdate(resumed);
      setResumeNotice(
        detail.kind === "agent-upgrade"
          ? "Retry queued behind a new safety delay. Reloading the current operation state."
          : "Resume accepted. Reloading the current operation state.",
      );
      const refreshed = await loadDetail();
      if (mountedRef.current && refreshed)
        setResumeNotice(
          detail.kind === "agent-upgrade"
            ? "Retry queued. It will not dispatch before the reported retry time."
            : "Operation resumed and current details reloaded.",
        );
    } catch (value) {
      if (mountedRef.current)
        setResumeError(value instanceof Error ? value.message : "Unable to resume this operation.");
    } finally {
      if (mountedRef.current) setResuming(false);
    }
  }

  const visibleTargets = detail?.targets.map((target) => friendlyTarget(target, targetNames)) ?? [];
  const visibleOperations = detail?.operations ?? [];
  const completed = detail?.progress?.completed ?? 0;
  const total = detail?.progress?.total ?? 0;
  const completion =
    compareWire(total, 0) > 0
      ? Math.min(100, Math.max(0, displayRatio(completed, total) * 100))
      : 0;
  const agentRetryQueued =
    detail?.agent_upgrade_diagnostics?.targets.some((target) => target.retry_queued) ?? false;

  return (
    <details className="activity-job" onToggle={toggle}>
      <summary>{detail ? "Operation progress" : "View operation progress"}</summary>
      <div className="activity-job-body">
        {loading && !detail && <SkeletonBlock label="Loading current operation details" />}
        {error && (
          <div className="activity-job-message is-error" role="alert">
            <p>Current operation details could not be loaded. {error}</p>
            <button
              type="button"
              className="button secondary"
              disabled={loading}
              onClick={() => void loadDetail()}
            >
              {loading ? "Retrying…" : "Retry"}
            </button>
          </div>
        )}
        {detail && (
          <>
            <header className="activity-job-header">
              <div>
                <span>Current state</span>
                <strong>{detail.summary}</strong>
              </div>
              {(!jobUpdatesAutomatically(detail) || error) && (
                <button
                  type="button"
                  className="button secondary"
                  disabled={loading || resuming}
                  onClick={() => void loadDetail()}
                >
                  {loading ? "Refreshing…" : "Refresh details"}
                </button>
              )}
            </header>
            {jobUpdatesAutomatically(detail) && (
              <p className="activity-job-live" role="status">
                <span aria-hidden="true" />
                {detail.state === "waiting"
                  ? "The Controller rechecks automatically; this view refreshes about once a minute."
                  : "Updates automatically while this operation is active."}
              </p>
            )}
            {detail.status_reason && (
              <div className="activity-job-reason">
                <span>State reason</span>
                <strong>{detail.status_reason}</strong>
              </div>
            )}
            {detail.projection_issue && (
              <p className="activity-job-message" role="status">
                {detail.projection_issue}
              </p>
            )}
            {detail.progress && (
              <section className="activity-job-progress" aria-label="Operation progress">
                <div>
                  <span>Completed</span>
                  <strong>{formatWire(detail.progress.completed)}</strong>
                </div>
                <div>
                  <span>Running</span>
                  <strong>{formatWire(detail.progress.running)}</strong>
                </div>
                <div>
                  <span>Failed</span>
                  <strong>{formatWire(detail.progress.failed)}</strong>
                </div>
                <div>
                  <span>Total</span>
                  <strong>{formatWire(detail.progress.total)}</strong>
                </div>
                <div
                  className="activity-job-progress-track"
                  role="img"
                  aria-label={`${formatWire(completed)} of ${formatWire(total)} operation steps completed`}
                >
                  <span style={{ width: `${completion}%` }} />
                </div>
              </section>
            )}
            {detail.kind !== "agent-upgrade" && (
              <p className="activity-job-attempt">
                Current attempt <strong>{formatWire(detail.current_attempt)}</strong>
              </p>
            )}
            <AgentUpgradeDiagnostics detail={detail} targetNames={targetNames} />
            {visibleTargets.length > 0 && (
              <section className="activity-job-targets" aria-label="Affected targets">
                <h3>Affected targets</h3>
                <ul>
                  {visibleTargets.map((target, index) => (
                    // biome-ignore lint/suspicious/noArrayIndexKey: the same target can repeat; the position tells the copies apart
                    <li key={`${detail.targets[index]}:${index}`}>{target}</li>
                  ))}
                </ul>
                {compareWire(detail.target_total, detail.targets.length) > 0 && (
                  <p>
                    Showing {detail.targets.length} of {formatWire(detail.target_total)} affected
                    targets.
                  </p>
                )}
              </section>
            )}
            {detail.progress &&
              "operation" in detail.progress &&
              detail.progress.operation != null && (
                <LibraryAvailabilityProgress
                  progress={availabilityProgress(detail.progress.operation)}
                />
              )}
            {visibleOperations.length > 0 && (
              <section className="activity-job-steps" aria-label="Operation steps">
                <h3>Operation steps</h3>
                <ul>
                  {visibleOperations.map((operation) => (
                    <li key={operation.id}>
                      <div>
                        <strong>{operation.summary}</strong>
                        <span>{friendlyTarget(operation.node_id, targetNames)}</span>
                      </div>
                      <StatusPill
                        tone={statusTone(
                          activityStatus({
                            ...event,
                            action: `operation.${operation.kind}.${operation.state}`,
                          }),
                        )}
                      >
                        {statusLabel(
                          activityStatus({
                            ...event,
                            action: `operation.${operation.kind}.${operation.state}`,
                          }),
                        )}
                      </StatusPill>
                      {operation.progress && (
                        <LibraryAvailabilityProgress
                          progress={availabilityProgress(operation.progress)}
                        />
                      )}{" "}
                      {operation.evidence_download && (
                        <DiagnosticDownload id={operation.id} attempt={operation.attempt} />
                      )}
                    </li>
                  ))}
                </ul>
                {detail.operation_total != null &&
                  compareWire(detail.operation_total, visibleOperations.length) > 0 && (
                    <p>
                      Showing {visibleOperations.length} of {formatWire(detail.operation_total)}{" "}
                      operation steps.
                    </p>
                  )}
              </section>
            )}
            {isOperatorWait(detail.state) &&
              (agentRetryQueued ? (
                <section className="activity-job-resume">
                  <div>
                    <strong>Retry queued behind safety delay</strong>
                    <p>{detail.agent_upgrade_diagnostics?.next_action}</p>
                  </div>
                </section>
              ) : (
                <section className="activity-job-resume">
                  <div>
                    <strong>Operator action required</strong>
                    <p>
                      {detail.agent_upgrade_diagnostics?.next_action ||
                        "This operation can be returned to the queue. Review the state reason and affected targets first."}
                    </p>
                  </div>
                  <button
                    type="button"
                    className="button"
                    disabled={resuming || loading}
                    onClick={() => void resume()}
                  >
                    {resuming
                      ? detail.kind === "agent-upgrade"
                        ? "Queuing…"
                        : "Resuming…"
                      : detail.kind === "agent-upgrade"
                        ? "Queue retry after inspection"
                        : "Resume operation"}
                  </button>
                </section>
              ))}
            {resumeNotice && (
              <p className="activity-job-message is-success" role="status">
                {resumeNotice}
              </p>
            )}
            {resumeError && (
              <p className="activity-job-message is-error" role="alert">
                Operation was not resumed. {resumeError}
              </p>
            )}
            <details className="activity-job-technical">
              <summary>Operation identifiers</summary>
              <dl>
                <CopyableValue label="Operation ID" value={detail.id} />
                <CopyableValue label="Authority revision" value={detail.authority_revision} />{" "}
                {detail.targets.map((target, index) => (
                  <CopyableValue
                    // biome-ignore lint/suspicious/noArrayIndexKey: the same target can repeat; the position tells the copies apart
                    key={`${target}:${index}`}
                    label={detail.targets.length === 1 ? "Target ID" : `Target ID ${index + 1}`}
                    value={target}
                  />
                ))}
              </dl>
            </details>
          </>
        )}
      </div>
    </details>
  );
}

function targetNameLookup(fleet: VisualFleetSnapshot | null): Map<string, string> {
  const names = new Map<string, string>();
  for (const node of fleet?.nodes ?? []) {
    names.set(node.id, nodeDisplayName(node));
  }
  return names;
}

function ActivityTimeline({
  api,
  events,
  now,
  onJobUpdate,
  onOperationUpdate,
  targetNames,
}: {
  api: ActivityApi;
  onOperationUpdate: (detail: OperationDetail) => void;
  events: ActivityRecord[];
  now: Date;
  onJobUpdate: (detail: JobDetail) => void;
  targetNames: Map<string, string>;
}) {
  return (
    <ol className="activity-timeline" aria-label="Activity timeline">
      {events.map((event, index) => {
        const status = activityStatus(event);
        return (
          <li
            // biome-ignore lint/suspicious/noArrayIndexKey: one request can appear in several events; the position tells them apart
            key={`${event.source}:${event.request_id}:${index}`}
            className={`activity-event is-${status}`}
          >
            <span className="activity-marker" aria-hidden="true" />
            <article>
              <header>
                <div>
                  <h2>{activityTitle(event)}</h2>
                </div>
                <StatusPill tone={statusTone(status)}>{statusLabel(status)}</StatusPill>
              </header>
              <div className="activity-event-meta">
                <span>
                  By <strong>{event.actor || "Unknown operator"}</strong>
                </span>
                <TargetSummary event={event} />
                <EventTime event={event} now={now} />
              </div>
              {event.source === "job" && (
                <JobProgressDetails
                  api={api}
                  event={event}
                  onUpdate={onJobUpdate}
                  targetNames={targetNames}
                />
              )}
              {event.operation && (
                <CanonicalOperationDetails
                  api={api}
                  detail={event.operation}
                  onUpdate={onOperationUpdate}
                />
              )}
              <TechnicalDetails event={event} />
            </article>
          </li>
        );
      })}
    </ol>
  );
}

function ActivityTable({
  api,
  events,
  now,
  onJobUpdate,
  onOperationUpdate,
  targetNames,
}: {
  api: ActivityApi;
  onOperationUpdate: (detail: OperationDetail) => void;
  events: ActivityRecord[];
  now: Date;
  onJobUpdate: (detail: JobDetail) => void;
  targetNames: Map<string, string>;
}) {
  return (
    <div className="activity-table-wrap">
      <table className="activity-table">
        <caption className="sr-only">Recorded operator and system activity</caption>
        <thead>
          <tr>
            <th scope="col">Event</th>
            <th scope="col">Status</th>
            <th scope="col">Operator</th>
            <th scope="col">When</th>
            <th scope="col">
              <span className="sr-only">Technical details</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {events.map((event, index) => {
            const status = activityStatus(event);
            return (
              // biome-ignore lint/suspicious/noArrayIndexKey: one request can appear in several events; the position tells them apart
              <tr key={`${event.source}:${event.request_id}:${index}`}>
                <td data-label="Event">
                  <strong>{activityTitle(event)}</strong>
                  <small>{activityCategory(event)}</small>
                  <TargetSummary compact event={event} />
                </td>
                <td data-label="Status">
                  <StatusPill tone={statusTone(status)}>{statusLabel(status)}</StatusPill>
                </td>
                <td data-label="Operator">{event.actor || "Unknown operator"}</td>
                <td data-label="When">
                  <EventTime event={event} now={now} />
                </td>
                <td className="activity-table-technical">
                  {event.source === "job" && (
                    <JobProgressDetails
                      api={api}
                      event={event}
                      onUpdate={onJobUpdate}
                      targetNames={targetNames}
                    />
                  )}
                  {event.operation && (
                    <CanonicalOperationDetails
                      api={api}
                      detail={event.operation}
                      onUpdate={onOperationUpdate}
                    />
                  )}
                  <TechnicalDetails event={event} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ActivityOverview({
  events,
  filtering,
  loadedCount,
}: {
  events: ActivityRecord[];
  filtering: boolean;
  loadedCount: number;
}) {
  const counts = events.reduce<Record<ActivityStatus, number>>(
    (result, event) => {
      result[activityStatus(event)] += 1;
      return result;
    },
    { recorded: 0, in_progress: 0, attention: 0, unsuccessful: 0, called_off: 0, unknown: 0 },
  );
  const scope = filtering
    ? `${events.length} matching ${events.length === 1 ? "event" : "events"} from ${loadedCount} loaded`
    : `${loadedCount} loaded ${loadedCount === 1 ? "event" : "events"}`;
  return (
    <section
      className="activity-overview"
      aria-label={filtering ? "Matching activity summary" : "Loaded activity summary"}
    >
      <p className="activity-overview-scope">Summary of {scope}.</p>
      <div>
        <span>Recorded</span>
        <strong>{counts.recorded}</strong>
      </div>
      <div>
        <span>In progress</span>
        <strong>{counts.in_progress}</strong>
      </div>
      <div>
        <span>Needs review</span>
        <strong>{counts.attention}</strong>
      </div>
      <div>
        <span>Unsuccessful</span>
        <strong>{counts.unsuccessful}</strong>
      </div>
      <div>
        <span>Cancelled</span>
        <strong>{counts.called_off}</strong>
      </div>
      <div>
        <span>Unknown state</span>
        <strong>{counts.unknown}</strong>
      </div>
    </section>
  );
}

const STANDALONE_JOB_PREFIX = "job:";

function canonicalRecord(detail: OperationDetail, names: Map<string, string>): ActivityRecord {
  // Standalone jobs (agent upgrades) are listed as operations; their
  // progress and resume live in the job detail.
  if (detail.id.startsWith(STANDALONE_JOB_PREFIX)) {
    return {
      request_id: detail.id.slice(STANDALONE_JOB_PREFIX.length),
      actor: "Vonk Forge",
      action: `operation.${detail.kind}.${detail.state}`,
      occurred_at: detail.created_at,
      targets: detail.node_ids,
      target_names: detail.node_ids.map((id) => names.get(id) ?? ""),
      summary: detail.summary,
      subject: detail.subject,
      source: "job",
    };
  }
  return {
    request_id: detail.id,
    actor: "Vonk Forge",
    action: `operation.${detail.kind}.${detail.state}`,
    occurred_at: detail.created_at,
    targets: detail.node_ids,
    target_names: detail.node_ids.map((id) => names.get(id) ?? ""),
    summary: detail.summary,
    subject: detail.subject,
    source: "operation",
    operation: detail,
  };
}

export function ModelCacheCancellationEvidence({ detail }: { detail: OperationDetail }) {
  if (!detail.kind.startsWith("model-cache.")) return null;
  const cancellation = detail.model_cache_cancellation;
  if (!cancellation)
    return detail.state === "cancelled" ? (
      <p style={{ margin: 0 }}>Writer stop evidence unavailable.</p>
    ) : null;
  return (
    <p style={{ margin: 0 }}>
      {cancellation.observation?.detail ??
        (detail.state === "cancelled"
          ? "Cancellation ended; stopping the writer remains unconfirmed."
          : "Cancellation requested; writer stop evidence unavailable.")}
    </p>
  );
}

function CanonicalOperationDetails({
  api,
  detail,
  onUpdate,
}: {
  api: ActivityApi;
  detail: OperationDetail;
  onUpdate: (detail: OperationDetail) => void;
}) {
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const refresh = useCallback(async () => {
    try {
      const current = await api.operation(detail.id);
      if (mounted.current) {
        onUpdate(current);
        setError("");
      }
    } catch (value) {
      if (mounted.current)
        setError(
          `Current progress could not be loaded. ${value instanceof Error ? value.message : "Try refreshing details."}`,
        );
    }
  }, [api, detail.id, onUpdate]);
  const active = stateUpdatesItself(detail.state);
  useEffect(() => {
    if (!active) return;
    let loading = false;
    const timer = window.setInterval(() => {
      if (loading) return;
      loading = true;
      void refresh().finally(() => {
        loading = false;
      });
    }, 5_000);
    return () => window.clearInterval(timer);
  }, [active, refresh]);
  const availability =
    detail.failure && "code" in detail.failure ? availabilityFailure(detail.failure) : undefined;
  return (
    <div className="activity-job-reason" style={{ gap: ".5rem" }}>
      <ModelCacheCancellationEvidence detail={detail} />
      {availability ? (
        <LibraryAvailabilityFeedback failure={availability} />
      ) : (
        detail.failure && (
          <>
            <strong>{failureSummary(detail.failure)}</strong>
            {"detail" in detail.failure && detail.failure.detail && (
              <p style={{ margin: 0 }}>{detail.failure.detail}</p>
            )}
          </>
        )
      )}
      {detail.evidence_download && <DiagnosticDownload id={detail.id} attempt={detail.attempt} />}
      {detail.progress && (
        <LibraryAvailabilityProgress progress={availabilityProgress(detail.progress)} />
      )}
      {detail.observation_unavailable && <p role="status">Details temporarily unavailable.</p>}
      {detail.projection_issues?.map((issue) => (
        <p key={issue.field} role="status" style={{ margin: 0 }}>
          {titleCase(issue.field)} unavailable: this observation requires{" "}
          {formatWire(issue.observed_bytes)} bytes; its reader budget is{" "}
          {formatWire(issue.budget_bytes)} bytes. Operation identity and state remain known. Refresh
          after the evidence changes.
        </p>
      ))}
      <p className="activity-job-attempt" style={{ margin: 0 }}>
        Attempt {formatWire(detail.attempt)}
        {detail.progress?.phase ? ` · ${titleCase(detail.progress.phase)}` : ""}
        {active ? " · Updates automatically" : ""}
      </p>
      {detail.recovery?.uncertain ||
      (detail.failure && "uncertain" in detail.failure && detail.failure.uncertain) ? (
        <p style={{ margin: 0 }}>
          Outcome uncertain.{" "}
          {detail.recovery?.explanation ?? "Inspect the observed state before recovery."}
        </p>
      ) : (
        detail.recovery?.explanation && <p style={{ margin: 0 }}>{detail.recovery.explanation}</p>
      )}
      {!operationEnded(detail.state) && (!active || error) && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: ".5rem" }}>
          <button type="button" className="button secondary" onClick={() => void refresh()}>
            Refresh operation
          </button>
        </div>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}

function eventTimestamp(event: ActivityRecord): number {
  if (!event.occurred_at) return 0;
  const timestamp = Date.parse(event.occurred_at);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

const STATUS_ORDER: Record<ActivityStatus, number> = {
  attention: 0,
  unsuccessful: 1,
  in_progress: 2,
  called_off: 2,
  unknown: 2,
  recorded: 2,
};

function sortActivityByAttention(left: ActivityRecord, right: ActivityRecord): number {
  const statusDifference = STATUS_ORDER[activityStatus(left)] - STATUS_ORDER[activityStatus(right)];
  return statusDifference || eventTimestamp(right) - eventTimestamp(left);
}

function sortActivityByTime(left: ActivityRecord, right: ActivityRecord): number {
  return eventTimestamp(right) - eventTimestamp(left);
}

/** The newest activity as short rows (what it was, to whom, how it ended), in the Activity page's own words. */
export function activityPreviewRows(
  operations: OperationDetail[],
  names: Map<string, string>,
  limit: number,
): {
  id: string;
  occurredAt: string | null;
  label: string;
  tone: ReturnType<typeof statusTone>;
  word: string;
}[] {
  return operations
    .map((operation) => canonicalRecord(operation, names))
    .sort(sortActivityByTime)
    .slice(0, limit)
    .map((record) => {
      const status = activityStatus(record);
      return {
        id: record.request_id,
        occurredAt: record.occurred_at ?? null,
        label: activityTitle(record),
        tone: statusTone(status),
        word: statusLabel(status),
      };
    });
}

/** How many of these operations ended unsuccessfully, by the same status the Activity filter uses. */
export function unsuccessfulCount(
  operations: OperationDetail[],
  names: Map<string, string>,
): number {
  return operations.filter(
    (operation) => activityStatus(canonicalRecord(operation, names)) === "unsuccessful",
  ).length;
}

export function ActivityPage({ api, now = new Date() }: { api: ActivityApi; now?: Date }) {
  const [events, setEvents] = useState<ActivityRecord[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [actor, setActor] = useState("");
  const [status, setStatus] = useState<ActivityStatus | "">(readStatus);
  const [filters, setFilters] = useState<ActivityFilters>(readFilters);
  const [requestText, setRequestText] = useState(() => readFilters().requestId ?? "");
  const [view, setView] = useState<ActivityView>(readViewPreference);
  const [sort, setSort] = useState<"recent" | "attention">("recent");
  const [targetNames, setTargetNames] = useState(new Map<string, string>());
  // The rarely used filters hide behind one button, open from the start when a link already set one.
  const [moreOpen, setMoreOpen] = useState(() => {
    const initial = readFilters();
    return Boolean(initial.state || initial.target || initial.requestId);
  });
  const [loadingMore, setLoadingMore] = useState(false);
  const [paginationError, setPaginationError] = useState("");
  const canonicalIds = useRef(new Set<string>());
  const [canonicalCount, setCanonicalCount] = useState(0);
  const [canonicalTotal, setCanonicalTotal] = useState<WireNumber>(0);
  const [canonicalCursor, setCanonicalCursor] = useState<string | null>(null);
  const [canonicalAvailable, setCanonicalAvailable] = useState(true);
  const requestGeneration = useRef(0);

  useEffect(() => {
    // The link opened the page on a status; from here the filter is the page's own, like search.
    const params = new URLSearchParams(location.search);
    if (!params.has("status")) return;
    params.delete("status");
    history.replaceState(null, "", `${location.pathname}${params.size ? `?${params}` : ""}`);
  }, []);
  useEffect(() => {
    const generation = ++requestGeneration.current;
    let active = true;
    setLoading(true);
    setLoadingMore(false);
    setError("");
    const controller = new AbortController();
    void Promise.allSettled([
      api.operations(undefined, controller.signal, filters),
      api.visualFleet(controller.signal).catch(() => null),
    ])
      .then(([canonicalResult, fleetResult]) => {
        if (!active) return;
        const canonical = canonicalResult.status === "fulfilled" ? canonicalResult.value : null;
        const canonicalError =
          canonicalResult.status === "rejected"
            ? canonicalResult.reason instanceof Error
              ? canonicalResult.reason.message
              : "Unable to load operations."
            : "";
        setCanonicalAvailable(Boolean(canonical));
        if (!canonical) {
          setEvents(null);
          setError(`Unable to load activity. ${canonicalError}`);
          return;
        }
        if (canonical.operations == null || canonical.total == null) {
          setCanonicalAvailable(false);
          setError(
            canonical.projection_issue ||
              "Operation membership is currently unknown. Refresh to observe it again.",
          );
          return;
        }
        canonicalIds.current = new Set(canonical.operations.map((operation) => operation.id));
        setCanonicalCount(canonicalIds.current.size);
        setCanonicalTotal(canonical.total);
        setCanonicalCursor(canonical.next_cursor ?? null);
        const fleet = fleetResult.status === "fulfilled" ? fleetResult.value : null;
        const names = targetNameLookup(fleet);
        setTargetNames(names);
        setPaginationError("");
        setEvents(
          canonical.operations
            .filter(
              (operation, index, all) =>
                all.findIndex((item) => item.id === operation.id) === index,
            )
            .map((operation) => canonicalRecord(operation, names))
            .sort(sortActivityByTime),
        );
      })
      .catch((value) => {
        if (!active) return;
        setEvents(null);
        setError(value instanceof Error ? value.message : "Unable to prepare activity.");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
      if (requestGeneration.current === generation) requestGeneration.current += 1;
    };
  }, [api, attempt, filters]);

  async function loadMoreOperations(): Promise<void> {
    if (!canonicalCursor || loading || loadingMore) return;
    const generation = requestGeneration.current;
    setLoadingMore(true);
    setPaginationError("");
    const [canonicalResult] = await Promise.allSettled([
      api.operations(canonicalCursor, undefined, filters),
    ]);
    if (requestGeneration.current !== generation) return;
    if (
      "reason" in canonicalResult &&
      canonicalResult.reason instanceof ApiError &&
      canonicalResult.reason.status === 422
    ) {
      // A rejected cursor will never be accepted: drop it and reload page 1.
      setCanonicalCursor(null);
      setLoadingMore(false);
      setAttempt((value) => value + 1);
      return;
    }
    const additional: ActivityRecord[] = [];
    const errors: string[] = [];
    if (canonicalResult.status === "fulfilled" && canonicalResult.value) {
      const next = canonicalResult.value;
      if (next.operations == null || next.total == null) {
        setPaginationError(
          next.projection_issue || "Older operation membership is currently unknown. Try again.",
        );
        setLoadingMore(false);
        return;
      }
      for (const operation of next.operations) {
        if (canonicalIds.current.has(operation.id)) continue;
        canonicalIds.current.add(operation.id);
        additional.push(canonicalRecord(operation, targetNames));
      }
      setCanonicalCount(canonicalIds.current.size);
      setCanonicalTotal(next.total);
      setCanonicalCursor(next.next_cursor ?? null);
    } else if (canonicalResult.status === "rejected")
      errors.push(
        `Older operations could not be loaded. ${canonicalResult.reason instanceof Error ? canonicalResult.reason.message : "Try again."}`,
      );
    setEvents((current) => [...(current ?? []), ...additional].sort(sortActivityByTime));
    setPaginationError(errors.join(" "));
    setLoadingMore(false);
  }

  const categories = useMemo(
    () => [...new Set((events ?? []).map(activityCategory))].sort(),
    [events],
  );
  const actors = useMemo(
    () => [...new Set((events ?? []).map((event) => event.actor).filter(Boolean))].sort(),
    [events],
  );
  useEffect(() => {
    if (category && !categories.includes(category)) setCategory("");
    if (actor && !actors.includes(actor)) setActor("");
  }, [actor, actors, categories, category]);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return (events ?? []).filter((event) => {
      if (category && activityCategory(event) !== category) return false;
      if (actor && event.actor !== actor) return false;
      if (status && activityStatus(event) !== status) return false;
      if (!normalized) return true;
      return [
        activityTitle(event),
        activityCategory(event),
        event.action,
        event.actor,
        event.request_id,
        failureText(event.operation?.failure),
        ...event.targets,
        ...(event.target_names ?? []),
      ].some((value) => value.toLocaleLowerCase().includes(normalized));
    });
  }, [actor, category, events, query, status]);
  const displayed = useMemo(
    () => [...filtered].sort(sort === "attention" ? sortActivityByAttention : sortActivityByTime),
    [filtered, sort],
  );

  const updateOperation = useCallback(
    (detail: JobDetail): void => {
      setEvents(
        (current) =>
          current
            ?.map((event) =>
              event.source === "job" && event.request_id === detail.id
                ? {
                    ...event,
                    action: `operation.${detail.kind}.${detail.state}`,
                    summary: detail.summary,
                    targets: detail.targets,
                    target_names: detail.targets.map((target) => targetNames.get(target) ?? ""),
                  }
                : event,
            )
            .sort(sortActivityByTime) ?? null,
      );
    },
    [targetNames],
  );

  const updateCanonicalOperation = useCallback(
    (detail: OperationDetail): void => {
      const record = canonicalRecord(detail, targetNames);
      const alreadyLoaded = canonicalIds.current.has(detail.id);
      canonicalIds.current.add(detail.id);
      setCanonicalCount(canonicalIds.current.size);
      if (!alreadyLoaded) setCanonicalTotal((current) => addWire(current, 1));
      setEvents((current) =>
        [
          ...(current ?? []).filter(
            (event) => !(event.source === "operation" && event.request_id === detail.id),
          ),
          record,
        ].sort(sortActivityByTime),
      );
    },
    [targetNames],
  );

  function chooseView(next: ActivityView): void {
    setView(next);
    try {
      localStorage.setItem(VIEW_PREFERENCE_KEY, next);
    } catch {
      /* Preferences are optional. */
    }
  }

  function changeFilters(next: ActivityFilters): void {
    setFilters(next);
    writeFilters(next);
  }

  function clearFilters(): void {
    setQuery("");
    setCategory("");
    setActor("");
    setStatus("");
    setRequestText("");
    changeFilters({});
  }

  const moreActive = [
    actor,
    filters.state,
    filters.target,
    filters.requestId,
    sort !== "recent",
  ].filter(Boolean).length;
  const filtering = Boolean(
    query.trim() ||
      category ||
      actor ||
      status ||
      filters.state ||
      filters.target ||
      filters.requestId,
  );
  return (
    <div className="activity-page">
      <PageHeader
        title="Activity"
        description="Meaningful control-plane changes, without technical identifiers by default."
        actions={
          <>
            <fieldset className="segmented group-reset" aria-label="Activity view">
              <button
                type="button"
                aria-pressed={view === "timeline"}
                onClick={() => chooseView("timeline")}
              >
                Timeline
              </button>
              <button
                type="button"
                aria-pressed={view === "table"}
                onClick={() => chooseView("table")}
              >
                Table
              </button>
            </fieldset>
            <button
              type="button"
              className="button secondary"
              disabled={loading || loadingMore}
              onClick={() => setAttempt((value) => value + 1)}
            >
              {loading && events ? "Refreshing…" : "Refresh activity"}
            </button>
          </>
        }
      />

      {events && events.length > 0 && (
        <ActivityOverview events={filtered} filtering={filtering} loadedCount={events.length} />
      )}

      {events && (
        <section className="library-pagination" aria-label="Activity history coverage">
          <p role="status">
            Showing {events.length} loaded {events.length === 1 ? "event" : "events"}.
            {canonicalCursor ? " Load older activity below." : ""}
          </p>
          <details className="activity-technical">
            <summary>History coverage</summary>
            <p>
              {canonicalAvailable
                ? `Loaded ${canonicalCount} of ${formatWire(canonicalTotal)} operations`
                : "Operations are unavailable"}
              . Summary counts and filters cover only these loaded records.
            </p>
          </details>
          {canonicalCursor && (
            <button
              type="button"
              className="button secondary"
              disabled={loading || loadingMore}
              onClick={() => void loadMoreOperations()}
            >
              {loadingMore ? "Loading older operations…" : "Load older operations"}
            </button>
          )}
          {canonicalAvailable &&
            !canonicalCursor &&
            compareWire(canonicalCount, canonicalTotal) < 0 && (
              <p role="status">
                The operations API reports additional records but did not provide a continuation
                cursor.
              </p>
            )}
          {paginationError && <p role="alert">{paginationError}</p>}
        </section>
      )}

      <section className="activity-controls" aria-label="Activity controls">
        <div className="activity-bar">
          <label className="activity-search">
            <span className="sr-only">Search activity</span>
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search activity: action, operator, or technical ID"
            />
          </label>
          <label>
            <span className="sr-only">Area</span>
            <select value={category} onChange={(event) => setCategory(event.target.value)}>
              <option value="">All areas</option>
              {categories.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">Status</span>
            <select
              value={status}
              onChange={(event) => setStatus(event.target.value as ActivityStatus | "")}
            >
              <option value="">All statuses</option>
              <option value="recorded">Recorded</option>
              <option value="in_progress">In progress</option>
              <option value="attention">Needs review</option>
              <option value="unsuccessful">Unsuccessful</option>
              <option value="called_off">Cancelled</option>
              <option value="unknown">Unknown state</option>
            </select>
          </label>
          <button
            type="button"
            className="button secondary"
            aria-expanded={moreOpen}
            aria-controls="activity-more-filters"
            onClick={() => setMoreOpen((open) => !open)}
          >
            More filters{moreActive > 0 ? ` (${moreActive})` : ""}
          </button>
        </div>
        <div className="activity-filters" id="activity-more-filters" hidden={!moreOpen}>
          <label>
            <span>Operator</span>
            <select value={actor} onChange={(event) => setActor(event.target.value)}>
              <option value="">All operators</option>
              {actors.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
          <label>
            <span>Operation state</span>
            <select
              value={filters.state ?? ""}
              onChange={(event) =>
                changeFilters({ ...filters, state: event.target.value || undefined })
              }
            >
              <option value="">All states</option>
              {OPERATION_STATES.map((value) => (
                <option key={value} value={value}>
                  {titleCase(value)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Spark</span>
            <select
              value={filters.target ?? ""}
              onChange={(event) =>
                changeFilters({ ...filters, target: event.target.value || undefined })
              }
            >
              <option value="">All Sparks</option>
              {[...targetNames].map(([id, name]) => (
                <option key={id} value={id}>
                  {name || id}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Request ID</span>
            <input
              value={requestText}
              placeholder="Full request ID"
              aria-invalid={requestText !== "" && !REQUEST_ID.test(requestText.trim())}
              onChange={(event) => {
                const value = event.target.value;
                setRequestText(value);
                const id = value.trim().toLowerCase();
                if (REQUEST_ID.test(id)) changeFilters({ ...filters, requestId: id });
                else if (!value.trim() && filters.requestId)
                  changeFilters({ ...filters, requestId: undefined });
              }}
            />
          </label>
          <label>
            <span>Sort</span>
            <select
              value={sort}
              onChange={(event) => setSort(event.target.value as "recent" | "attention")}
            >
              <option value="recent">Most recent first</option>
              <option value="attention">Needs attention first</option>
            </select>
          </label>
        </div>
        <div className="activity-control-footer">
          <span role="status">
            {events
              ? `${filtered.length} of ${events.length} loaded ${events.length === 1 ? "event" : "events"} · ${sort === "attention" ? "Needs-review and unsuccessful events first" : "Most recent events first"}`
              : "Loading activity"}
          </span>
          {filtering && filtered.length > 0 && (
            <button type="button" className="activity-clear" onClick={clearFilters}>
              Clear filters
            </button>
          )}
        </div>
      </section>

      {loading && !events && !error && <SkeletonRows columns={4} label="Loading activity" />}
      {error && (
        <section className="activity-state is-error" role="alert">
          <div>
            <strong>Activity unavailable</strong>
            <p>{error}</p>
          </div>
          <button
            type="button"
            className="button secondary"
            disabled={loading || loadingMore}
            onClick={() => setAttempt((value) => value + 1)}
          >
            Retry
          </button>
        </section>
      )}
      {!loading && !error && events && filtered.length === 0 && (
        <EmptyState
          title="No activity yet"
          description="Activity lists the changes made to your fleet, keys and library."
          filtered={filtering}
          onClearFilters={clearFilters}
          action={{ label: "Refresh", onClick: () => setAttempt((value) => value + 1) }}
        />
      )}
      {displayed.length > 0 &&
        (view === "timeline" ? (
          <ActivityTimeline
            api={api}
            events={displayed}
            now={now}
            onJobUpdate={updateOperation}
            onOperationUpdate={updateCanonicalOperation}
            targetNames={targetNames}
          />
        ) : (
          <ActivityTable
            api={api}
            events={displayed}
            now={now}
            onJobUpdate={updateOperation}
            onOperationUpdate={updateCanonicalOperation}
            targetNames={targetNames}
          />
        ))}
    </div>
  );
}
