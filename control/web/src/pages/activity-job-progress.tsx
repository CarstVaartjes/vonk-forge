import { NoReading } from "../components/ui/no-reading";
import { compareWire, displayRatio, formatWire } from "../api/contract-numeric";
import {
  availabilityProgress,
  LibraryAvailabilityProgress,
} from "../components/library/library-availability-progress";
import { LifecycleState } from "../api/vocabulary.generated";
import { useCallback, useEffect, useRef, useState } from "react";
import type { SyntheticEvent } from "react";
import type { JobDetail } from "../api/types";
import { SkeletonBlock } from "../components/ui/skeleton";
import { StatusPill } from "../components/ui/status-pill";
import { exactTime } from "../lib/time";
import {
  activityStatus,
  isOperatorWait,
  stateUpdatesItself,
  statusLabel,
  statusTone,
} from "./activity-status";
import { type ActivityApi, type ActivityRecord, friendlyTarget } from "./activity-records";
import { CopyableValue, DiagnosticDownload } from "./activity-event-parts";

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
                <dd>{target.observed_identity.version || <NoReading />}</dd>
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

export function JobProgressDetails({
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
