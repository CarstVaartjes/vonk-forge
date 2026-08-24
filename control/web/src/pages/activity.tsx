import {useEffect, useMemo, useRef, useState} from "react";
import type {AuditSummary, ControlApi, JobSummary, LibrarySnapshot, VisualFleetSnapshot} from "../api/types";
import {StatusPill} from "../components/status-pill";
import {nodeDisplayName} from "../lib/fleet";

type ActivityView = "timeline" | "table";
type ActivityStatus = "recorded" | "in_progress" | "attention" | "unsuccessful";
type ActivityRecord = AuditSummary & {occurred_at?: string | null; source: "audit" | "operation"; target_names?: string[]};
type TimestampedJob = JobSummary & {created_at?: string};

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
    if (/failed|cancelled|error/.test(state)) return "unsuccessful";
    if (/waiting-for-operator|uncertain/.test(state)) return "attention";
    if (/pending|planned|queued|running|starting|stopping|compensating/.test(state)) return "in_progress";
  }
  if (/(?:^|\.)(?:failed|rejected|throttled|denied|error)(?:\.|$)/.test(event.action)) return "unsuccessful";
  if (/(?:^|\.)(?:uncertain|warning|conflict|stale)(?:\.|$)/.test(event.action)) return "attention";
  return "recorded";
}

function statusLabel(status: ActivityStatus): string {
  if (status === "unsuccessful") return "Unsuccessful";
  if (status === "attention") return "Needs review";
  if (status === "in_progress") return "In progress";
  return "Recorded";
}

function statusTone(status: ActivityStatus): "healthy" | "warning" | "danger" | "info" {
  if (status === "unsuccessful") return "danger";
  if (status === "attention") return "warning";
  if (status === "in_progress") return "info";
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
    <dd className="activity-copy-value"><code>{value}</code><button type="button" className="activity-copy" onClick={() => void copy()} aria-label={`Copy ${label.toLowerCase()}`}>{copyState === "copied" ? "Copied" : "Copy"}</button></dd>
    {copyState === "failed" && <dd className="activity-copy-error" role="status">Clipboard access is unavailable. Select the value to copy it.</dd>}
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
  return target.startsWith("spk_") ? "Unavailable Spark" : "Unavailable object";
}

function TargetSummary({compact = false, event}: {compact?: boolean; event: ActivityRecord}) {
  if (event.targets.length === 0) return null;
  const names = event.targets.map((target, index) => event.target_names?.[index] || unavailableTargetLabel(target));
  const content = <><span>{names.length === 1 ? "Target" : "Targets"}</span> <strong>{names.join(" · ")}</strong></>;
  return compact ? <small className="activity-target-summary">{content}</small> : <span className="activity-target-summary">{content}</span>;
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

function ActivityTimeline({events, now}: {events: ActivityRecord[]; now: Date}) {
  return <ol className="activity-timeline" aria-label="Activity timeline">
    {events.map((event, index) => {
      const status = activityStatus(event);
      return <li key={`${event.request_id}:${event.action}:${index}`} className={`activity-event is-${status}`}>
        <span className="activity-marker" aria-hidden="true"/>
        <article>
          <header>
            <div><span className="activity-category">{activityCategory(event)}</span><h2>{activityActionLabel(event.action)}</h2></div>
            <StatusPill tone={statusTone(status)}>{statusLabel(status)}</StatusPill>
          </header>
          <div className="activity-event-meta"><span>By <strong>{event.actor || "Unknown operator"}</strong></span><TargetSummary event={event}/><EventTime event={event} now={now}/></div>
          <TechnicalDetails event={event}/>
        </article>
      </li>;
    })}
  </ol>;
}

function ActivityTable({events, now}: {events: ActivityRecord[]; now: Date}) {
  return <div className="activity-table-wrap"><table className="activity-table">
    <caption className="sr-only">Recorded operator and system activity</caption>
    <thead><tr><th scope="col">Event</th><th scope="col">Status</th><th scope="col">Operator</th><th scope="col">When</th><th scope="col"><span className="sr-only">Technical details</span></th></tr></thead>
    <tbody>{events.map((event, index) => {
      const status = activityStatus(event);
      return <tr key={`${event.request_id}:${event.action}:${index}`}>
        <td data-label="Event"><strong>{activityActionLabel(event.action)}</strong><small>{activityCategory(event)}</small><TargetSummary compact event={event}/></td>
        <td data-label="Status"><StatusPill tone={statusTone(status)}>{statusLabel(status)}</StatusPill></td>
        <td data-label="Operator">{event.actor || "Unknown operator"}</td>
        <td data-label="When"><EventTime event={event} now={now}/></td>
        <td className="activity-table-technical"><TechnicalDetails event={event}/></td>
      </tr>;
    })}</tbody>
  </table></div>;
}

function ActivityOverview({events}: {events: ActivityRecord[]}) {
  const counts = events.reduce<Record<ActivityStatus, number>>((result, event) => {
    result[activityStatus(event)] += 1;
    return result;
  }, {recorded: 0, in_progress: 0, attention: 0, unsuccessful: 0});
  const total = Math.max(events.length, 1);
  return <section className="activity-overview" aria-label="Loaded activity summary">
    <div><span>Recorded</span><strong>{counts.recorded}</strong></div>
    <div><span>In progress</span><strong>{counts.in_progress}</strong></div>
    <div><span>Needs review</span><strong>{counts.attention}</strong></div>
    <div><span>Unsuccessful</span><strong>{counts.unsuccessful}</strong></div>
    <div className="activity-outcome-bar" role="img" aria-label={`${counts.recorded} recorded, ${counts.in_progress} in progress, ${counts.attention} need review, ${counts.unsuccessful} unsuccessful`}>
      <span className="is-recorded" style={{width: `${counts.recorded / total * 100}%`}}/>
      <span className="is-in-progress" style={{width: `${counts.in_progress / total * 100}%`}}/>
      <span className="is-attention" style={{width: `${counts.attention / total * 100}%`}}/>
      <span className="is-unsuccessful" style={{width: `${counts.unsuccessful / total * 100}%`}}/>
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

export function ActivityPage({api, now = new Date()}: {api: Pick<ControlApi, "audit" | "jobs" | "librarySnapshot" | "visualFleet">; now?: Date}) {
  const [events, setEvents] = useState<ActivityRecord[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [actor, setActor] = useState("");
  const [status, setStatus] = useState<ActivityStatus | "">("");
  const [view, setView] = useState<ActivityView>(readViewPreference);
  const [auditCount, setAuditCount] = useState(0);
  const [loadedJobCount, setLoadedJobCount] = useState(0);
  const [jobTotal, setJobTotal] = useState(0);
  const [jobCursor, setJobCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [paginationError, setPaginationError] = useState("");
  const operationIds = useRef(new Set<string>());
  const requestGeneration = useRef(0);

  useEffect(() => {
    const generation = ++requestGeneration.current;
    let active = true;
    setLoading(true);
    setError("");
    const controller = new AbortController();
    void Promise.all([
      api.audit(),
      api.jobs(),
      api.visualFleet(controller.signal).catch(() => null),
      api.librarySnapshot(undefined, controller.signal).catch(() => null),
    ]).then(([audit, operations, fleet, library]) => {
      if (!active) return;
      const names = targetNameLookup(fleet, library);
      operationIds.current = new Set(operations.jobs.map(job => job.id));
      setAuditCount(audit.events.length);
      setLoadedJobCount(operationIds.current.size);
      setJobTotal(operations.total);
      setJobCursor(operations.next_cursor ?? null);
      setPaginationError("");
      setEvents([
        ...audit.events.map(event => ({...event, source: "audit" as const, target_names: event.targets.map(target => names.get(target) ?? "")})),
        ...operations.jobs.filter((job, index, jobs) => jobs.findIndex(candidate => candidate.id === job.id) === index).map(job => operationRecord(job as TimestampedJob)),
      ].sort((left, right) => eventTimestamp(right) - eventTimestamp(left)));
    }).catch(value => {
      if (active) setError(value instanceof Error ? value.message : "Unable to load activity.");
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
      setEvents(current => [...(current ?? []), ...additional.map(job => operationRecord(job as TimestampedJob))].sort((left, right) => eventTimestamp(right) - eventTimestamp(left)));
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
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    return (events ?? []).filter(event => {
      if (category && activityCategory(event) !== category) return false;
      if (actor && event.actor !== actor) return false;
      if (status && activityStatus(event) !== status) return false;
      if (!normalized) return true;
      return [activityActionLabel(event.action), activityCategory(event), event.action, event.actor, event.request_id, event.authority_revision ?? "", ...event.targets]
        .some(value => value.toLocaleLowerCase().includes(normalized));
    });
  }, [actor, category, events, query, status]);

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

  const filtering = Boolean(query || category || actor || status);
  return <div className="activity-page">
    <header className="activity-hero">
      <div><p className="fleet-kicker">Operations history</p><h1 tabIndex={-1}>Activity</h1><p>Understand meaningful control-plane changes without exposing technical identifiers by default.</p></div>
      <button type="button" className="button secondary" disabled={loading || loadingMore} onClick={() => setAttempt(value => value + 1)}>{loading && events ? "Refreshing…" : "Refresh activity"}</button>
    </header>

    {events && events.length > 0 && <ActivityOverview events={events}/>}

    {events && <section className="library-pagination" aria-label="Activity history coverage">
      <p role="status">Loaded {auditCount} audit {auditCount === 1 ? "record" : "records"} from the latest-100 API window and {loadedJobCount} of {jobTotal} operations. Summary counts and filters cover only these loaded records; older audit history is not available from this API.</p>
      {jobCursor && <button type="button" className="button secondary" disabled={loading || loadingMore} onClick={() => void loadMoreOperations()}>{loadingMore ? "Loading older operations…" : "Load older operations"}</button>}
      {!jobCursor && loadedJobCount < jobTotal && <p role="status">The operations API reports additional records but did not provide a continuation cursor.</p>}
      {paginationError && <p role="alert">{paginationError}</p>}
    </section>}

    <section className="activity-controls" aria-label="Activity controls">
      <label className="activity-search"><span>Search activity</span><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Action, operator, or technical ID"/></label>
      <div className="activity-filters">
        <label><span>Area</span><select value={category} onChange={event => setCategory(event.target.value)}><option value="">All areas</option>{categories.map(value => <option key={value}>{value}</option>)}</select></label>
        <label><span>Operator</span><select value={actor} onChange={event => setActor(event.target.value)}><option value="">All operators</option>{actors.map(value => <option key={value}>{value}</option>)}</select></label>
        <label><span>Status</span><select value={status} onChange={event => setStatus(event.target.value as ActivityStatus | "")}><option value="">All statuses</option><option value="recorded">Recorded</option><option value="in_progress">In progress</option><option value="attention">Needs review</option><option value="unsuccessful">Unsuccessful</option></select></label>
      </div>
      <div className="activity-control-footer">
        <span role="status">{events ? `${filtered.length} of ${events.length} loaded ${events.length === 1 ? "event" : "events"}` : "Loading activity"}</span>
        {filtering && filtered.length > 0 && <button type="button" className="activity-clear" onClick={clearFilters}>Clear filters</button>}
        <div className="activity-view-switcher" role="group" aria-label="Activity view">
          <button type="button" aria-pressed={view === "timeline"} onClick={() => chooseView("timeline")}>Timeline</button>
          <button type="button" aria-pressed={view === "table"} onClick={() => chooseView("table")}>Table</button>
        </div>
      </div>
    </section>

    {loading && !events && <section className="activity-state" role="status"><strong>Loading activity…</strong><p>Reading the latest operator and system events.</p></section>}
    {error && <section className="activity-state is-error" role="alert"><div><strong>Activity unavailable</strong><p>{error}</p></div><button type="button" className="button secondary" onClick={() => setAttempt(value => value + 1)}>Try again</button></section>}
    {!loading && !error && events?.length === 0 && <section className="activity-state"><strong>No activity in the loaded window</strong><p>No audit or operation records were returned by the current API windows.</p></section>}
    {events && filtered.length === 0 && events.length > 0 && <section className="activity-state"><strong>No matching activity</strong><p>Try a broader search or remove one or more filters.</p><button type="button" className="button secondary" onClick={clearFilters}>Clear filters</button></section>}
    {filtered.length > 0 && (view === "timeline" ? <ActivityTimeline events={filtered} now={now}/> : <ActivityTable events={filtered} now={now}/>)}
  </div>;
}
