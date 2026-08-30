import {useCallback, useEffect, useId, useMemo, useRef, useState} from "react";
import type {SyntheticEvent} from "react";
import type {AuditSummary, ControlApi, JobDetail, JobSummary, LibrarySnapshot, VisualFleetSnapshot} from "../api/types";
import {StatusPill} from "../components/status-pill";
import {nodeDisplayName} from "../lib/fleet";

type ActivityView = "timeline" | "table";
type ActivityStatus = "recorded" | "in_progress" | "attention" | "unsuccessful" | "unknown";
type ActivityRecord = AuditSummary & {occurred_at?: string | null; source: "audit" | "operation"; target_names?: string[]};
type TimestampedJob = JobSummary & {created_at?: string};
type ActivityApi = Pick<ControlApi, "audit" | "job" | "jobs" | "librarySnapshot" | "resumeJob" | "visualFleet">;

const VIEW_PREFERENCE_KEY = "vonk.activity.view";

const ACTION_LABELS: Record<string, string> = {
  "agent.enrollment.grant.create": "Created enrollment grant",
  "agent.enrollment.submit.approved": "Approved Spark enrollment",
  "agent.enrollment.submit.rejected": "Rejected Spark enrollment",
  "agent.enrollment.submit.uncertain": "Spark enrollment needs review",
  "agent.node.revoke": "Revoked Spark access",
  "authority.change.submit": "Submitted authority change",
  "auth.login.failed": "Sign-in failed",
  "auth.login.succeeded": "Signed in",
  "auth.login.throttled": "Sign-in rate limited",
  "auth.logout": "Signed out",
  "catalog.entity.create": "Created catalog item",
  "catalog.entity.resolve": "Resolved catalog item",
  "catalog.entity.revise": "Revised catalog item",
  "catalog.global.import": "Imported public catalog item",
  "catalog.publication.export": "Exported catalog publication",
  "catalog.recipe.create": "Created recipe",
  "catalog.recipe.fork": "Forked recipe",
  "catalog.recipe.resolve": "Resolved recipe",
  "catalog.recipe.update": "Updated recipe",
  "catalog.recipe_library.import": "Imported recipe library",
  "catalog.source_bundle.upload": "Uploaded source bundle",
  "catalog.test_report.attach": "Attached validation report",
  "catalog.workload_run.import": "Imported workload recipe",
  "catalog.workload_run.resolve": "Resolved workload recipe",
  "fleet.revoke": "Revoked Spark access",
  "job.resume": "Resumed operation",
  "recipe.build": "Built recipe image",
  "recipe.image.distribute": "Distributed recipe image",
  "recipe.install": "Installed recipe",
  "recipe.mapping.create": "Created recipe placement",
  "recipe.retry": "Retried recipe operation",
  "recipe.start": "Started recipe",
  "recipe.stop": "Stopped recipe",
  "recipe.uninstall": "Uninstalled recipe",
};

const CATEGORY_LABELS: Record<string, string> = {
  agent: "Sparks",
  auth: "Authentication",
  authority: "Authority",
  catalog: "Catalog",
  fleet: "Fleet",
  job: "Operations",
  library: "Library",
  recipe: "Recipes",
  operation: "Operations",
};

function titleCase(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, character => character.toUpperCase());
}

export function activityActionLabel(action: string): string {
  if (action.startsWith("operation.")) {
    const parts = action.split(".").slice(1);
    const state = parts.pop() ?? "unknown";
    const kind = titleCase(parts.join(" ")) || "Operation";
    const stateLabels: Record<string, string> = {
      cancelled: "Cancelled",
      compensated: "Recovered",
      compensating: "Recovering",
      completed: "Completed",
      failed: "Failed",
      pending: "Pending",
      planned: "Planned",
      queued: "Queued",
      running: "Running",
      succeeded: "Completed",
      uncertain: "Needs review",
      "waiting-for-operator": "Waiting for operator",
    };
    return `${kind} · ${stateLabels[state] ?? titleCase(state)}`;
  }
  const explicit = ACTION_LABELS[action];
  if (explicit) return explicit;
  const parts = action.split(".").filter(Boolean);
  const useful = parts.length > 1 ? parts.slice(1) : parts;
  const label = titleCase(useful.join(" "));
  return label ? `${label.charAt(0).toUpperCase()}${label.slice(1).toLowerCase()}` : "Recorded activity";
}

export function activityCategory(event: AuditSummary): string {
  const category = event.action.split(".")[0] || "other";
  return CATEGORY_LABELS[category] ?? titleCase(category);
}

export function activityStatus(event: AuditSummary): ActivityStatus {
  if (event.action.startsWith("operation.")) {
    const state = event.action.split(".").at(-1) ?? "";
    const knownStates: Record<string, ActivityStatus> = {
      cancelled: "unsuccessful",
      canceled: "unsuccessful",
      error: "unsuccessful",
      expired: "unsuccessful",
      failed: "unsuccessful",
      uncertain: "attention",
      "waiting-for-operator": "attention",
      compensating: "in_progress",
      pending: "in_progress",
      planned: "in_progress",
      queued: "in_progress",
      running: "in_progress",
      starting: "in_progress",
      stopping: "in_progress",
      compensated: "recorded",
      completed: "recorded",
      succeeded: "recorded",
    };
    return knownStates[state] ?? "unknown";
  }
  if (/(?:^|\.)(?:failed|rejected|throttled|denied|error)(?:\.|$)/.test(event.action)) return "unsuccessful";
  if (/(?:^|\.)(?:uncertain|warning|conflict|stale)(?:\.|$)/.test(event.action)) return "attention";
  return "recorded";
}

function statusLabel(status: ActivityStatus): string {
  if (status === "unsuccessful") return "Unsuccessful";
  if (status === "attention") return "Needs review";
  if (status === "in_progress") return "In progress";
  if (status === "unknown") return "Unknown state";
  return "Recorded";
}

function statusTone(status: ActivityStatus): "neutral" | "healthy" | "warning" | "danger" | "info" {
  if (status === "unsuccessful") return "danger";
  if (status === "attention") return "warning";
  if (status === "in_progress") return "info";
  if (status === "unknown") return "neutral";
  return "healthy";
}

function readViewPreference(): ActivityView {
  try {
    return localStorage.getItem(VIEW_PREFERENCE_KEY) === "table" ? "table" : "timeline";
  } catch {
    return "timeline";
  }
}

function exactTime(value: string): string | null {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return null;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "long",
  }).format(parsed);
}

export function relativeTime(value: string, now: Date): string | null {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return null;
  const seconds = Math.round((parsed.getTime() - now.getTime()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, {numeric: "auto"});
  const ranges: Array<[number, Intl.RelativeTimeFormatUnit]> = [
    [60, "second"],
    [60, "minute"],
    [24, "hour"],
    [7, "day"],
  ];
  let valueAtRange = seconds;
  for (const [boundary, unit] of ranges) {
    if (Math.abs(valueAtRange) < boundary) return formatter.format(valueAtRange, unit);
    valueAtRange = Math.round(valueAtRange / boundary);
  }
  return formatter.format(valueAtRange, "week");
}

function EventTime({event, now}: {event: ActivityRecord; now: Date}) {
  if (!event.occurred_at) return <span className="activity-time is-unavailable"><strong>Time not recorded</strong><small>The current audit record has no timestamp</small></span>;
  const exact = exactTime(event.occurred_at);
  const relative = relativeTime(event.occurred_at, now);
  if (!exact || !relative) return <span className="activity-time is-unavailable"><strong>Time not recorded</strong><small>The audit timestamp is invalid</small></span>;
  return <time className="activity-time" dateTime={event.occurred_at} title={exact}><strong>{relative}</strong><small>{exact}</small></time>;
}

function CopyableValue({label, value}: {label: string; value?: string | null}) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const statusId = useId();
  if (!value) return <div><dt>{label}</dt><dd>Not recorded</dd></div>;
  async function copy(): Promise<void> {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(value!);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }
  return <div>
    <dt>{label}</dt>
    <dd className="activity-copy-value"><code>{value}</code><button type="button" className="activity-copy" aria-describedby={statusId} onClick={() => void copy()} aria-label={`Copy ${label.toLowerCase()}`}>{copyState === "copied" ? "Copied" : "Copy"}</button></dd>
    {copyState === "failed" && <dd className="activity-copy-error">Clipboard access is unavailable. Select the value to copy it.</dd>}
    <dd className="sr-only" id={statusId} aria-live="polite">{copyState === "copied" ? `${label} copied` : copyState === "failed" ? `Could not copy ${label.toLocaleLowerCase()}` : ""}</dd>
  </div>;
}

function TechnicalDetails({event}: {event: ActivityRecord}) {
  return <details className="activity-technical">
    <summary>Technical details</summary>
    <dl>
      <CopyableValue label={event.source === "operation" ? "Operation ID" : "Request ID"} value={event.request_id}/>
      {event.source === "audit" && <CopyableValue label="Authority revision" value={event.authority_revision}/>}
      {event.targets.length === 0
        ? <div><dt>Targets</dt><dd>None recorded</dd></div>
        : event.targets.map((target, index) => <CopyableValue key={`${target}:${index}`} label={event.targets.length === 1 ? "Target" : `Target ${index + 1}`} value={target}/>)}
    </dl>
  </details>;
}

function unavailableTargetLabel(target: string): string {
  return target.startsWith("spk_") ? "Spark no longer registered" : "Target not in current inventory";
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
  if (historicalSparks > 0) names.push(`${historicalSparks} historical ${historicalSparks === 1 ? "Spark" : "Sparks"}`);
  if (historicalTargets > 0) names.push(`${historicalTargets} historical ${historicalTargets === 1 ? "target" : "targets"}`);
  return names;
}

function TargetSummary({compact = false, event}: {compact?: boolean; event: ActivityRecord}) {
  if (event.targets.length === 0) return null;
  const names = targetSummaryNames(event);
  const content = <><span>{names.length === 1 ? "Target" : "Targets"}</span> <strong>{names.join(" · ")}</strong></>;
  return compact ? <small className="activity-target-summary">{content}</small> : <span className="activity-target-summary">{content}</span>;
}

const LIVE_JOB_STATES = new Set(["compensating", "pending", "planned", "queued", "running", "starting", "stopping"]);

function jobUpdatesAutomatically(detail: JobDetail): boolean {
  return LIVE_JOB_STATES.has(detail.state) || (
    detail.state === "waiting-for-operator"
    && (detail.agent_upgrade_diagnostics?.targets.some(target => target.retry_queued) ?? false)
  );
}

function friendlyTarget(target: string, names: Map<string, string>): string {
  return names.get(target) || unavailableTargetLabel(target);
}

function AgentUpgradeDiagnostics({detail, targetNames}: {detail: JobDetail; targetNames: Map<string, string>}) {
  const diagnostics = detail.agent_upgrade_diagnostics;
  if (!diagnostics) return null;
  const expected = diagnostics.expected_identity;
  return <section className="activity-upgrade-diagnostics" aria-label="Agent upgrade diagnosis">
    <header><div><span>Expected release</span><strong>{expected.version || "Not recorded"}</strong></div><StatusPill tone={diagnostics.targets.every(target => target.target_proven) ? "healthy" : "warning"}>{diagnostics.targets.every(target => target.target_proven) ? "Identity proven" : "Identity not proven"}</StatusPill></header>
    <dl className="activity-upgrade-expected"><CopyableValue label="Target binary digest" value={expected.binary_digest}/><CopyableValue label="Target build digest" value={expected.build_digest}/></dl>
    <ul>{diagnostics.targets.map(target => <li key={target.node_id}>
      <div className="activity-upgrade-target"><strong>{friendlyTarget(target.node_id, targetNames)}</strong><span>{target.attempts} install {target.attempts === 1 ? "attempt" : "attempts"} · {target.target_proven ? "exact target reported" : "exact target not reported"}</span></div>
      <dl><div><dt>Observed version</dt><dd>{target.observed_identity.version || "Not reported"}</dd></div><CopyableValue label="Observed binary digest" value={target.observed_identity.binary_digest}/><CopyableValue label="Observed build digest" value={target.observed_identity.build_digest}/>{target.retry_not_before && <div><dt>{target.retry_queued ? "Controller retry not before" : "Retry not before"}</dt><dd><time dateTime={target.retry_not_before}>{exactTime(target.retry_not_before) || target.retry_not_before}</time></dd></div>}</dl>
      {target.raw_reason && <details><summary>Raw helper evidence</summary><code>{target.raw_reason}</code></details>}
    </li>)}</ul>
    {diagnostics.legacy_generic_ambiguous && <p className="activity-upgrade-ambiguity"><strong>Legacy helper response is ambiguous.</strong> It does not prove that authorization or download failed, and it does not prove that the package installed. The exact runtime identity remains the success gate.</p>}
  </section>;
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
    return () => { mountedRef.current = false; };
  }, []);

  const loadDetail = useCallback(async (background = false): Promise<JobDetail | undefined> => {
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
      if (mountedRef.current) setError(value instanceof Error ? value.message : "Unable to load current operation details.");
      return undefined;
    } finally {
      loadingRef.current = false;
      if (mountedRef.current) setLoading(false);
    }
  }, [api, event.request_id, onUpdate]);

  useEffect(() => {
    if (!open || !detail || !jobUpdatesAutomatically(detail)) return undefined;
    const interval = window.setInterval(() => loadDetail(true), 5_000);
    return () => window.clearInterval(interval);
  }, [detail, loadDetail, open]);

  function toggle(event: SyntheticEvent<HTMLDetailsElement>): void {
    const nextOpen = event.currentTarget.open;
    setOpen(nextOpen);
    if (nextOpen && !detail && !loadingRef.current) void loadDetail();
  }

  async function resume(): Promise<void> {
    if (!detail || detail.state !== "waiting-for-operator" || resuming) return;
    setResuming(true);
    setResumeError("");
    setResumeNotice("");
    try {
      const response = await api.resumeJob(event.request_id);
      if (!mountedRef.current) return;
      const resumed = {...detail, state: response.state};
      setDetail(resumed);
      onUpdate(resumed);
      setResumeNotice(detail.kind === "agent-upgrade" ? "Retry queued behind a new safety delay. Reloading the current operation state." : "Resume accepted. Reloading the current operation state.");
      const refreshed = await loadDetail();
      if (mountedRef.current && refreshed) setResumeNotice(detail.kind === "agent-upgrade" ? "Retry queued. It will not dispatch before the reported retry time." : "Operation resumed and current details reloaded.");
    } catch (value) {
      if (mountedRef.current) setResumeError(value instanceof Error ? value.message : "Unable to resume this operation.");
    } finally {
      if (mountedRef.current) setResuming(false);
    }
  }

  const visibleTargets = detail?.targets.map(target => friendlyTarget(target, targetNames)) ?? [];
  const visibleOperations = detail?.operations ?? [];
  const completed = detail?.progress.completed ?? 0;
  const total = detail?.progress.total ?? 0;
  const completion = total > 0 ? Math.min(100, Math.max(0, completed / total * 100)) : 0;
  const agentRetryQueued = detail?.agent_upgrade_diagnostics?.targets.some(target => target.retry_queued) ?? false;

  return <details className="activity-job" onToggle={toggle}>
    <summary>{detail ? "Operation progress" : "View operation progress"}</summary>
    <div className="activity-job-body">
      {loading && !detail && <p role="status">Loading current operation details…</p>}
      {error && <div className="activity-job-message is-error" role="alert"><p>Current operation details could not be loaded. {error}</p><button type="button" className="button secondary" disabled={loading} onClick={() => void loadDetail()}>{loading ? "Retrying…" : "Try again"}</button></div>}
      {detail && <>
        <header className="activity-job-header">
          <div><span>Current state</span><strong>{titleCase(detail.state)}</strong></div>
          <button type="button" className="button secondary" disabled={loading || resuming} onClick={() => void loadDetail()}>{loading ? "Refreshing…" : "Refresh details"}</button>
        </header>
        {jobUpdatesAutomatically(detail) && <p className="activity-job-live" role="status"><span aria-hidden="true"/>Updates automatically while this operation is active.</p>}
        {detail.status_reason && <div className="activity-job-reason"><span>State reason</span><strong>{detail.status_reason}</strong></div>}
        <section className="activity-job-progress" aria-label="Operation progress">
          <div><span>Completed</span><strong>{detail.progress.completed}</strong></div>
          <div><span>Running</span><strong>{detail.progress.running}</strong></div>
          <div><span>Failed</span><strong>{detail.progress.failed}</strong></div>
          <div><span>Total</span><strong>{detail.progress.total}</strong></div>
          <div className="activity-job-progress-track" role="img" aria-label={`${completed} of ${total} operation steps completed`}><span style={{width: `${completion}%`}}/></div>
        </section>
        {detail.kind !== "agent-upgrade" && <p className="activity-job-attempt">Current attempt <strong>{detail.current_attempt}</strong></p>}
        <AgentUpgradeDiagnostics detail={detail} targetNames={targetNames}/>
        {visibleTargets.length > 0 && <section className="activity-job-targets" aria-label="Affected targets"><h3>Affected targets</h3><ul>{visibleTargets.map((target, index) => <li key={`${detail.targets[index]}:${index}`}>{target}</li>)}</ul>{detail.target_total > detail.targets.length && <p>Showing {detail.targets.length} of {detail.target_total} affected targets.</p>}</section>}
        {visibleOperations.length > 0 && <section className="activity-job-steps" aria-label="Operation steps"><h3>Operation steps</h3><ul>{visibleOperations.map(operation => <li key={operation.id}><div><strong>{titleCase(operation.kind)}</strong><span>{friendlyTarget(operation.node_id, targetNames)}</span></div><StatusPill tone={statusTone(activityStatus({...event, action: `operation.${operation.kind}.${operation.state}`}))}>{titleCase(operation.state)}</StatusPill>{operation.progress?.phase && <small>Phase: {operation.progress.phase}</small>}</li>)}</ul>{detail.operation_total > detail.operations.length && <p>Showing {detail.operations.length} of {detail.operation_total} operation steps.</p>}</section>}
        {detail.state === "waiting-for-operator" && (agentRetryQueued ? <section className="activity-job-resume"><div><strong>Retry queued behind safety delay</strong><p>{detail.agent_upgrade_diagnostics?.next_action}</p></div></section> : <section className="activity-job-resume"><div><strong>Operator action required</strong><p>{detail.agent_upgrade_diagnostics?.next_action || "This operation can be returned to the queue. Review the state reason and affected targets first."}</p></div><button type="button" className="button" disabled={resuming || loading} onClick={() => void resume()}>{resuming ? detail.kind === "agent-upgrade" ? "Queuing…" : "Resuming…" : detail.kind === "agent-upgrade" ? "Queue retry after inspection" : "Resume operation"}</button></section>)}
        {resumeNotice && <p className="activity-job-message is-success" role="status">{resumeNotice}</p>}
        {resumeError && <p className="activity-job-message is-error" role="alert">Operation was not resumed. {resumeError}</p>}
        <details className="activity-job-technical"><summary>Operation identifiers</summary><dl><CopyableValue label="Operation ID" value={detail.id}/><CopyableValue label="Authority revision" value={detail.authority_revision}/>{detail.reconciliation_id && <CopyableValue label="Reconciliation ID" value={detail.reconciliation_id}/>} {detail.targets.map((target, index) => <CopyableValue key={`${target}:${index}`} label={detail.targets.length === 1 ? "Target ID" : `Target ID ${index + 1}`} value={target}/>)}</dl></details>
      </>}
    </div>
  </details>;
}

function targetNameLookup(fleet: VisualFleetSnapshot | null, library: LibrarySnapshot | null): Map<string, string> {
  const names = new Map<string, string>();
  for (const node of fleet?.nodes ?? []) {
    names.set(node.id, nodeDisplayName(node));
  }
  const recipes = [
    ...(library?.models.flatMap(model => model.recipes) ?? []),
    ...(library?.unlinked_recipes ?? []),
  ];
  for (const recipe of recipes) {
    const title = recipe.title.trim() && recipe.title.trim() !== recipe.recipe_id ? recipe.title.trim() : "Unnamed recipe";
    names.set(recipe.recipe_id, title);
    if (recipe.selected_revision) names.set(recipe.selected_revision.id, `${title} revision ${recipe.selected_revision.revision_number}`);
    recipe.installations.forEach((installation, index) => names.set(installation.installation_id, `${title} installation ${index + 1}`));
    recipe.runs.forEach((run, index) => names.set(run.run_id, `${title} run ${index + 1}`));
  }
  return names;
}

function ActivityTimeline({api, events, now, onJobUpdate, targetNames}: {api: Pick<ActivityApi, "job" | "resumeJob">; events: ActivityRecord[]; now: Date; onJobUpdate: (detail: JobDetail) => void; targetNames: Map<string, string>}) {
  return <ol className="activity-timeline" aria-label="Activity timeline">
    {events.map((event, index) => {
      const status = activityStatus(event);
      return <li key={`${event.source}:${event.request_id}:${index}`} className={`activity-event is-${status}`}>
        <span className="activity-marker" aria-hidden="true"/>
        <article>
          <header>
            <div><span className="activity-category">{activityCategory(event)}</span><h2>{activityActionLabel(event.action)}</h2></div>
            <StatusPill tone={statusTone(status)}>{statusLabel(status)}</StatusPill>
          </header>
          <div className="activity-event-meta"><span>By <strong>{event.actor || "Unknown operator"}</strong></span><TargetSummary event={event}/><EventTime event={event} now={now}/></div>
          {event.source === "operation" && <JobProgressDetails api={api} event={event} onUpdate={onJobUpdate} targetNames={targetNames}/>}
          <TechnicalDetails event={event}/>
        </article>
      </li>;
    })}
  </ol>;
}

function ActivityTable({api, events, now, onJobUpdate, targetNames}: {api: Pick<ActivityApi, "job" | "resumeJob">; events: ActivityRecord[]; now: Date; onJobUpdate: (detail: JobDetail) => void; targetNames: Map<string, string>}) {
  return <div className="activity-table-wrap"><table className="activity-table">
    <caption className="sr-only">Recorded operator and system activity</caption>
    <thead><tr><th scope="col">Event</th><th scope="col">Status</th><th scope="col">Operator</th><th scope="col">When</th><th scope="col"><span className="sr-only">Technical details</span></th></tr></thead>
    <tbody>{events.map((event, index) => {
      const status = activityStatus(event);
      return <tr key={`${event.source}:${event.request_id}:${index}`}>
        <td data-label="Event"><strong>{activityActionLabel(event.action)}</strong><small>{activityCategory(event)}</small><TargetSummary compact event={event}/></td>
        <td data-label="Status"><StatusPill tone={statusTone(status)}>{statusLabel(status)}</StatusPill></td>
        <td data-label="Operator">{event.actor || "Unknown operator"}</td>
        <td data-label="When"><EventTime event={event} now={now}/></td>
        <td className="activity-table-technical">{event.source === "operation" && <JobProgressDetails api={api} event={event} onUpdate={onJobUpdate} targetNames={targetNames}/>}<TechnicalDetails event={event}/></td>
      </tr>;
    })}</tbody>
  </table></div>;
}

function ActivityOverview({events, filtering, loadedCount}: {events: ActivityRecord[]; filtering: boolean; loadedCount: number}) {
  const counts = events.reduce<Record<ActivityStatus, number>>((result, event) => {
    result[activityStatus(event)] += 1;
    return result;
  }, {recorded: 0, in_progress: 0, attention: 0, unsuccessful: 0, unknown: 0});
  const total = Math.max(events.length, 1);
  const scope = filtering
    ? `${events.length} matching ${events.length === 1 ? "event" : "events"} from ${loadedCount} loaded`
    : `${loadedCount} loaded ${loadedCount === 1 ? "event" : "events"}`;
  return <section className="activity-overview" aria-label={filtering ? "Matching activity summary" : "Loaded activity summary"}>
    <p className="activity-overview-scope">Summary of {scope}.</p>
    <div><span>Recorded</span><strong>{counts.recorded}</strong></div>
    <div><span>In progress</span><strong>{counts.in_progress}</strong></div>
    <div><span>Needs review</span><strong>{counts.attention}</strong></div>
    <div><span>Unsuccessful</span><strong>{counts.unsuccessful}</strong></div>
    <div><span>Unknown state</span><strong>{counts.unknown}</strong></div>
    <div className="activity-outcome-bar" role="img" aria-label={`${counts.recorded} recorded, ${counts.in_progress} in progress, ${counts.attention} need review, ${counts.unsuccessful} unsuccessful, ${counts.unknown} unknown`}>
      <span className="is-recorded" style={{width: `${counts.recorded / total * 100}%`}}/>
      <span className="is-in-progress" style={{width: `${counts.in_progress / total * 100}%`}}/>
      <span className="is-attention" style={{width: `${counts.attention / total * 100}%`}}/>
      <span className="is-unsuccessful" style={{width: `${counts.unsuccessful / total * 100}%`}}/>
      <span className="is-unknown" style={{width: `${counts.unknown / total * 100}%`}}/>
    </div>
  </section>;
}

function operationRecord(job: TimestampedJob): ActivityRecord {
  return {
    request_id: job.id,
    actor: "Vonk Forge",
    action: `operation.${job.kind}.${job.state}`,
    occurred_at: job.created_at,
    targets: [],
    source: "operation",
  };
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

export function ActivityPage({api, now = new Date()}: {api: ActivityApi; now?: Date}) {
  const [events, setEvents] = useState<ActivityRecord[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [actor, setActor] = useState("");
  const [status, setStatus] = useState<ActivityStatus | "">("");
  const [view, setView] = useState<ActivityView>(readViewPreference);
  const [sort, setSort] = useState<"recent" | "attention">("recent");
  const [targetNames, setTargetNames] = useState(new Map<string, string>());
  const [auditCount, setAuditCount] = useState(0);
  const [loadedJobCount, setLoadedJobCount] = useState(0);
  const [jobTotal, setJobTotal] = useState(0);
  const [jobCursor, setJobCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [paginationError, setPaginationError] = useState("");
  const [partialWarning, setPartialWarning] = useState("");
  const [auditAvailable, setAuditAvailable] = useState(true);
  const [jobsAvailable, setJobsAvailable] = useState(true);
  const operationIds = useRef(new Set<string>());
  const requestGeneration = useRef(0);

  useEffect(() => {
    const generation = ++requestGeneration.current;
    let active = true;
    setLoading(true);
    setLoadingMore(false);
    setError("");
    setPartialWarning("");
    const controller = new AbortController();
    void Promise.allSettled([
      api.audit(),
      api.jobs(),
      api.visualFleet(controller.signal).catch(() => null),
      api.librarySnapshot(undefined, controller.signal).catch(() => null),
    ]).then(([auditResult, operationsResult, fleetResult, libraryResult]) => {
      if (!active) return;
      const audit = auditResult.status === "fulfilled" ? auditResult.value : null;
      const operations = operationsResult.status === "fulfilled" ? operationsResult.value : null;
      const auditError = auditResult.status === "rejected" ? (auditResult.reason instanceof Error ? auditResult.reason.message : "Unable to load audit history.") : "";
      const operationsError = operationsResult.status === "rejected" ? (operationsResult.reason instanceof Error ? operationsResult.reason.message : "Unable to load operations.") : "";
      setAuditAvailable(Boolean(audit));
      setJobsAvailable(Boolean(operations));
      if (!audit && !operations) {
        setEvents(null);
        setError(`Unable to load audit history or operations. ${[auditError, operationsError].filter(Boolean).join(" ")}`);
        return;
      }
      if (!audit) setPartialWarning(`Audit history could not be loaded (${auditError}). Showing operations only.`);
      if (!operations) setPartialWarning(`Operations could not be loaded (${operationsError}). Showing audit history only.`);
      const fleet = fleetResult.status === "fulfilled" ? fleetResult.value : null;
      const library = libraryResult.status === "fulfilled" ? libraryResult.value : null;
      const names = targetNameLookup(fleet, library);
      setTargetNames(names);
      operationIds.current = new Set((operations?.jobs ?? []).map(job => job.id));
      setAuditCount(audit?.events.length ?? 0);
      setLoadedJobCount(operationIds.current.size);
      setJobTotal(operations?.total ?? 0);
      setJobCursor(operations?.next_cursor ?? null);
      setPaginationError("");
      setEvents([
        ...(audit?.events ?? []).map(event => ({...event, source: "audit" as const, target_names: event.targets.map(target => names.get(target) ?? "")})),
        ...(operations?.jobs ?? []).filter((job, index, jobs) => jobs.findIndex(candidate => candidate.id === job.id) === index).map(job => operationRecord(job as TimestampedJob)),
      ].sort(sortActivityByTime));
    }).catch(value => {
      if (!active) return;
      setEvents(null);
      setError(value instanceof Error ? value.message : "Unable to prepare activity.");
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => {
      active = false;
      controller.abort();
      if (requestGeneration.current === generation) requestGeneration.current += 1;
    };
  }, [api, attempt]);

  async function loadMoreOperations(): Promise<void> {
    if (!jobCursor || loading || loadingMore) return;
    const cursor = jobCursor;
    const generation = requestGeneration.current;
    setLoadingMore(true);
    setPaginationError("");
    try {
      const next = await api.jobs(cursor);
      if (requestGeneration.current !== generation) return;
      const additional = next.jobs.filter(job => !operationIds.current.has(job.id));
      additional.forEach(job => operationIds.current.add(job.id));
      setEvents(current => [...(current ?? []), ...additional.map(job => operationRecord(job as TimestampedJob))].sort(sortActivityByTime));
      setLoadedJobCount(operationIds.current.size);
      setJobTotal(next.total);
      setJobCursor(next.next_cursor ?? null);
    } catch (value) {
      if (requestGeneration.current === generation) setPaginationError(value instanceof Error ? value.message : "Unable to load older operations.");
    } finally {
      if (requestGeneration.current === generation) setLoadingMore(false);
    }
  }

  const categories = useMemo(() => [...new Set((events ?? []).map(activityCategory))].sort(), [events]);
  const actors = useMemo(() => [...new Set((events ?? []).map(event => event.actor).filter(Boolean))].sort(), [events]);
  useEffect(() => {
    if (category && !categories.includes(category)) setCategory("");
    if (actor && !actors.includes(actor)) setActor("");
  }, [actor, actors, categories, category]);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return (events ?? []).filter(event => {
      if (category && activityCategory(event) !== category) return false;
      if (actor && event.actor !== actor) return false;
      if (status && activityStatus(event) !== status) return false;
      if (!normalized) return true;
      return [activityActionLabel(event.action), activityCategory(event), event.action, event.actor, event.request_id, event.authority_revision ?? "", ...event.targets, ...(event.target_names ?? [])]
        .some(value => value.toLocaleLowerCase().includes(normalized));
    });
  }, [actor, category, events, query, status]);
  const displayed = useMemo(() => [...filtered].sort(sort === "attention" ? sortActivityByAttention : sortActivityByTime), [filtered, sort]);

  const updateOperation = useCallback((detail: JobDetail): void => {
    setEvents(current => current?.map(event => event.source === "operation" && event.request_id === detail.id
      ? {...event, action: `operation.${detail.kind}.${detail.state}`, targets: detail.targets, target_names: detail.targets.map(target => targetNames.get(target) ?? "")}
      : event).sort(sortActivityByTime) ?? null);
  }, [targetNames]);

  function chooseView(next: ActivityView): void {
    setView(next);
    try { localStorage.setItem(VIEW_PREFERENCE_KEY, next); } catch { /* Preferences are optional. */ }
  }

  function clearFilters(): void {
    setQuery("");
    setCategory("");
    setActor("");
    setStatus("");
  }

  const filtering = Boolean(query.trim() || category || actor || status);
  return <div className="activity-page">
    <header className="activity-hero">
      <div><h1 tabIndex={-1}>Activity</h1><p>Understand meaningful control-plane changes without exposing technical identifiers by default.</p></div>
      <button type="button" className="button secondary" disabled={loading || loadingMore} onClick={() => setAttempt(value => value + 1)}>{loading && events ? "Refreshing…" : "Refresh activity"}</button>
    </header>

    {events && events.length > 0 && <ActivityOverview events={filtered} filtering={filtering} loadedCount={events.length}/>}

    {events && <section className="library-pagination" aria-label="Activity history coverage">
      <p role="status">{auditAvailable ? `Loaded ${auditCount} audit ${auditCount === 1 ? "record" : "records"} from the latest-100 API window` : "Audit history is unavailable"} and {jobsAvailable ? `${loadedJobCount} of ${jobTotal} operations` : "operations are unavailable"}. Summary counts and filters cover only these loaded records; older audit history is not available from this API.</p>
      {jobCursor && <button type="button" className="button secondary" disabled={loading || loadingMore} onClick={() => void loadMoreOperations()}>{loadingMore ? "Loading older operations…" : "Load older operations"}</button>}
      {jobsAvailable && !jobCursor && loadedJobCount < jobTotal && <p role="status">The operations API reports additional records but did not provide a continuation cursor.</p>}
      {paginationError && <p role="alert">{paginationError}</p>}
    </section>}

    {partialWarning && <section className="activity-source-warning" role="alert"><div><strong>Some activity could not be loaded</strong><p>{partialWarning}</p></div><button type="button" className="button secondary" disabled={loading || loadingMore} onClick={() => setAttempt(value => value + 1)}>Retry all sources</button></section>}

    <section className="activity-controls" aria-label="Activity controls">
      <label className="activity-search"><span>Search activity</span><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Action, operator, or technical ID"/></label>
      <div className="activity-filters">
        <label><span>Area</span><select value={category} onChange={event => setCategory(event.target.value)}><option value="">All areas</option>{categories.map(value => <option key={value}>{value}</option>)}</select></label>
        <label><span>Operator</span><select value={actor} onChange={event => setActor(event.target.value)}><option value="">All operators</option>{actors.map(value => <option key={value}>{value}</option>)}</select></label>
        <label><span>Status</span><select value={status} onChange={event => setStatus(event.target.value as ActivityStatus | "")}><option value="">All statuses</option><option value="recorded">Recorded</option><option value="in_progress">In progress</option><option value="attention">Needs review</option><option value="unsuccessful">Unsuccessful</option><option value="unknown">Unknown state</option></select></label>
        <label><span>Sort</span><select value={sort} onChange={event => setSort(event.target.value as "recent" | "attention")}><option value="recent">Most recent first</option><option value="attention">Needs attention first</option></select></label>
      </div>
      <div className="activity-control-footer">
        <span role="status">{events ? `${filtered.length} of ${events.length} loaded ${events.length === 1 ? "event" : "events"} · ${sort === "attention" ? "Needs-review and unsuccessful events first" : "Most recent events first"}` : "Loading activity"}</span>
        {filtering && filtered.length > 0 && <button type="button" className="activity-clear" onClick={clearFilters}>Clear filters</button>}
        <div className="activity-view-switcher" role="group" aria-label="Activity view">
          <button type="button" aria-pressed={view === "timeline"} onClick={() => chooseView("timeline")}>Timeline</button>
          <button type="button" aria-pressed={view === "table"} onClick={() => chooseView("table")}>Table</button>
        </div>
      </div>
    </section>

    {loading && !events && <section className="activity-state" role="status"><strong>Loading activity…</strong><p>Reading the latest operator and system events.</p></section>}
    {error && <section className="activity-state is-error" role="alert"><div><strong>Activity unavailable</strong><p>{error}</p></div><button type="button" className="button secondary" disabled={loading || loadingMore} onClick={() => setAttempt(value => value + 1)}>Try again</button></section>}
    {!loading && !error && events?.length === 0 && <section className="activity-state"><strong>{partialWarning ? "No activity from available sources" : "No activity in the loaded window"}</strong><p>{partialWarning ? "The available activity source returned no records. Retry to check the unavailable source." : "No audit or operation records were returned by the current API windows."}</p></section>}
    {events && filtered.length === 0 && events.length > 0 && <section className="activity-state"><strong>No matching activity</strong><p>Try a broader search or remove one or more filters.</p><button type="button" className="button secondary" onClick={clearFilters}>Clear filters</button></section>}
    {displayed.length > 0 && (view === "timeline" ? <ActivityTimeline api={api} events={displayed} now={now} onJobUpdate={updateOperation} targetNames={targetNames}/> : <ActivityTable api={api} events={displayed} now={now} onJobUpdate={updateOperation} targetNames={targetNames}/>)}
  </div>;
}
