import {useEffect, useMemo, useRef, useState} from "react";
import type {KeyboardEvent as ReactKeyboardEvent} from "react";
import type {ControlApi, EnrollmentGrantResponse} from "../api/types";
import {CopyButton} from "../components/copy-button";
import {FleetCompactView, FleetTopologyView} from "../components/fleet-views";
import {Meter} from "../components/meter";
import {NodeCard} from "../components/node-card";
import {NodeDetail} from "../components/node-detail";
import {StatusPill} from "../components/status-pill";
import {useFleetStream} from "../hooks/use-fleet-stream";
import {usePoliteAnnouncement} from "../hooks/use-polite-announcement";
import {formatBytes, summarizeFleet} from "../lib/fleet";

export const ENROLLMENT_GRANT_TTL_SECONDS = 900;
export const FLEET_VIEW_STORAGE_KEY = "vonk-forge:fleet-view";
export type FleetViewMode = "cards" | "compact" | "topology";

const FLEET_VIEWS: readonly {label: string; value: FleetViewMode; icon: "cards" | "compact" | "topology"}[] = [
  {label: "Cards", value: "cards", icon: "cards"},
  {label: "Compact", value: "compact", icon: "compact"},
  {label: "Topology", value: "topology", icon: "topology"},
];

function storedFleetView(): FleetViewMode {
  try {
    const value = localStorage.getItem(FLEET_VIEW_STORAGE_KEY);
    return value === "compact" || value === "topology" ? value : "cards";
  } catch {
    return "cards";
  }
}

function ViewIcon({kind}: {kind: "cards" | "compact" | "topology"}) {
  if (kind === "cards") return <svg aria-hidden="true" viewBox="0 0 20 20"><rect x="2" y="2" width="7" height="7" rx="1"/><rect x="11" y="2" width="7" height="7" rx="1"/><rect x="2" y="11" width="7" height="7" rx="1"/><rect x="11" y="11" width="7" height="7" rx="1"/></svg>;
  if (kind === "compact") return <svg aria-hidden="true" viewBox="0 0 20 20"><path d="M3 5h14M3 10h14M3 15h14"/><circle cx="3" cy="5" r="1"/><circle cx="3" cy="10" r="1"/><circle cx="3" cy="15" r="1"/></svg>;
  return <svg aria-hidden="true" viewBox="0 0 20 20"><circle cx="10" cy="4" r="2"/><circle cx="4" cy="15" r="2"/><circle cx="16" cy="15" r="2"/><path d="M10 6v3M10 9 4 13M10 9l6 4"/></svg>;
}

function countLabel(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

function bootstrapCommand(grant: EnrollmentGrantResponse): string {
  const address = grant.controller_address;
  return address
    ? `curl -fsSL https://install.vonkforge.ai/spark | VONK_CONTROLLER_ADDRESS=${address} sh`
    : "curl -fsSL https://install.vonkforge.ai/spark | sh";
}

function connectionPresentation(connection: ReturnType<typeof useFleetStream>["connection"]) {
  switch (connection) {
    case "live": return {label: "Live connection", tone: "healthy" as const};
    case "reconnecting": return {label: "Reconnecting", tone: "warning" as const};
    case "polling": return {label: "Polling fallback", tone: "warning" as const};
    case "connecting": return {label: "Connecting", tone: "neutral" as const};
  }
}

function ExpiryCountdown({expiresAt}: {expiresAt: string}) {
  const [currentTime, setCurrentTime] = useState(Date.now());
  useEffect(() => {
    const interval = window.setInterval(() => setCurrentTime(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, []);
  const expiry = new Date(expiresAt);
  if (!Number.isFinite(expiry.getTime())) return <span>Expiry time unavailable</span>;
  const remaining = Math.max(0, Math.ceil((expiry.getTime() - currentTime) / 1_000));
  const days = Math.floor(remaining / 86_400);
  const hours = Math.floor((remaining % 86_400) / 3_600);
  const minutes = Math.floor((remaining % 3_600) / 60);
  const seconds = remaining % 60;
  const display = days > 0
    ? `${days}d ${hours}h`
    : hours > 0
      ? `${hours}h ${minutes}m`
      : `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return <time dateTime={expiry.toISOString()} title={expiry.toLocaleString([], {dateStyle: "medium", timeStyle: "long"})}>{remaining === 0 ? "Grant expired" : `Expires in ${display}`}</time>;
}

function CopyField({label, value, code = false}: {label: string; value: string; code?: boolean}) {
  return <span className="grant-copy-field">
    {code ? <code>{value}</code> : <span>{value}</span>}
    <CopyButton label={label} value={value}/>
  </span>;
}

function SparkOnboarding({api, onBusyChange, onClose}: {api: ControlApi; onBusyChange?(busy: boolean): void; onClose(): void}) {
  const [grant, setGrant] = useState<EnrollmentGrantResponse>();
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const dialog = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const creatingGrant = useRef(false);
  const keepGrantButton = useRef<HTMLButtonElement>(null);
  const grantSuccess = useRef<HTMLDivElement>(null);
  const protectsGrant = creating || Boolean(grant);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeButton.current?.focus();
    return () => { document.body.style.overflow = previousOverflow; };
  }, []);

  useEffect(() => { if (grant) grantSuccess.current?.focus(); }, [grant]);

  useEffect(() => {
    onBusyChange?.(protectsGrant);
    return () => onBusyChange?.(false);
  }, [onBusyChange, protectsGrant]);

  useEffect(() => {
    if (!protectsGrant) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    addEventListener("beforeunload", warnBeforeUnload);
    return () => removeEventListener("beforeunload", warnBeforeUnload);
  }, [protectsGrant]);

  useEffect(() => {
    if (confirmDiscard) keepGrantButton.current?.focus();
  }, [confirmDiscard]);

  function requestClose() {
    if (creating) return;
    if (grant) {
      setConfirmDiscard(true);
      return;
    }
    onClose();
  }

  function handleKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      if (confirmDiscard) {
        setConfirmDiscard(false);
        closeButton.current?.focus();
      } else {
        requestClose();
      }
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>("a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary") ?? [])];
    if (focusable.length === 0) return;
    const first = focusable[0]!;
    const last = focusable.at(-1)!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function createGrant() {
    if (creatingGrant.current || grant) return;
    creatingGrant.current = true;
    setCreating(true);
    setError("");
    try {
      setGrant(await api.createEnrollmentGrant(ENROLLMENT_GRANT_TTL_SECONDS));
    } catch (value) {
      setError(value instanceof Error ? value.message : "The enrollment grant could not be created.");
    } finally {
      creatingGrant.current = false;
      setCreating(false);
    }
  }

  return <div className="library-dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) requestClose(); }}>
    <div ref={dialog} className="library-action-dialog onboarding-dialog" role="dialog" aria-modal="true" aria-labelledby="spark-onboarding-title" aria-busy={creating || undefined} onKeyDown={handleKeyDown}>
      <header><div><p className="fleet-kicker">Secure node enrollment</p><h3 id="spark-onboarding-title">Add Spark</h3><p className="dialog-subtitle">Issue a short-lived pairing authorization.</p></div><button ref={closeButton} type="button" className="icon-button" disabled={creating} onClick={requestClose} aria-label="Close Add Spark">×</button></header>
      <div className="library-action-dialog-body">
        {!grant && <>
          <ol className="onboarding-steps" aria-label="Spark onboarding steps"><li><span className="onboarding-step-number" aria-hidden="true">1</span><div><strong>Create grant</strong><span>Generate a one-time authorization here.</span></div></li><li><span className="onboarding-step-number" aria-hidden="true">2</span><div><strong>Run installer</strong><span>Run the public installer on the Spark.</span></div></li><li><span className="onboarding-step-number" aria-hidden="true">3</span><div><strong>Enter credentials</strong><span>Provide the values shown when prompted.</span></div></li></ol>
          <p className="onboarding-guidance">Use a descriptive hostname on the Spark; Fleet uses it as the friendly fallback name. The Spark generates its immutable <code>spk_…</code> identity locally. The installer verifies the release before requesting sudo and pins the controller CA.</p>
          {error && <p role="alert" className="dialog-error">{error}</p>}
          {creating && <p id="grant-creation-status" role="status" className="onboarding-locked-note"><strong>Creating the one-time authorization…</strong> Keep this window open until the setup values appear.</p>}
        </>}
        {grant && <>
          <div ref={grantSuccess} className="grant-success" tabIndex={-1}><span className="success-mark" aria-hidden="true">✓</span><div><strong>One-time command ready</strong><ExpiryCountdown expiresAt={grant.expires_at}/></div></div>
          <p className="grant-secret-warning"><strong>Keep these setup values private.</strong> The one-time token authorizes one Spark and will not be shown again after closing.</p>
          <p>Run this command on the Spark. Enter the enrollment URL, CA fingerprint, and one-time token below when prompted. The installer pins the controller CA and configures the NAS LAN route automatically.</p>
          <div className="onboarding-command-block"><code className="onboarding-command" tabIndex={0} aria-label="Spark installer command">{bootstrapCommand(grant)}</code><CopyButton label="installer command" value={bootstrapCommand(grant)}/></div>
          <dl className="grant-facts"><div><dt>One-time token</dt><dd><CopyField label="one-time token" value={grant.token} code/></dd></div><div><dt>Controller</dt><dd><CopyField label="controller endpoint" value={grant.controller_endpoint}/></dd></div><div><dt>Enrollment</dt><dd><CopyField label="enrollment endpoint" value={grant.enrollment_endpoint}/></dd></div>{grant.controller_address && <div><dt>NAS LAN address</dt><dd><CopyField label="NAS LAN address" value={grant.controller_address} code/></dd></div>}<div><dt>CA fingerprint</dt><dd><CopyField label="CA fingerprint" value={grant.ca_fingerprint} code/></dd></div></dl>
          {confirmDiscard && <section className="grant-discard-confirmation" role="alert" aria-labelledby="discard-grant-title">
            <div><strong id="discard-grant-title">Discard this one-time grant?</strong><p>This token will not be shown again. Only leave if you saved every value or no longer intend to enroll this Spark.</p></div>
            <div className="grant-discard-actions"><button ref={keepGrantButton} type="button" className="button secondary" onClick={() => setConfirmDiscard(false)}>Keep grant open</button><button type="button" className="button danger" onClick={onClose}>Discard grant</button></div>
          </section>}
        </>}
      </div>
      <footer>{grant ? <button type="button" className="button" onClick={onClose}>I saved these values — Done</button> : <><button type="button" className="button secondary" disabled={creating} onClick={requestClose}>Cancel</button><button type="button" className="button" disabled={creating} aria-describedby={creating ? "grant-creation-status" : undefined} onClick={() => void createGrant()}>{creating ? "Creating…" : "Create one-time enrollment command"}</button></>}</footer>
    </div>
  </div>;
}


export function FleetPage({api, onBusyChange}: {api: ControlApi; onBusyChange?(busy: boolean): void}) {
  const fleet = useFleetStream(api);
  const [selectedNodeId, setSelectedNodeId] = useState<string>();
  const [onboarding, setOnboarding] = useState(false);
  const [viewMode, setViewMode] = useState<FleetViewMode>(storedFleetView);
  const [gpuTrends, setGpuTrends] = useState<Record<string, number[]>>({});
  const detailTrigger = useRef<HTMLElement | null>(null);
  const onboardingTrigger = useRef<HTMLElement | null>(null);
  const lastTrendSample = useRef(new Map<string, string>());

  useEffect(() => {
    const snapshot = fleet.snapshot;
    if (!snapshot) return;
    setGpuTrends(current => {
      let changed = false;
      const next = {...current};
      for (const node of snapshot.nodes) {
        const sample = node.telemetry?.sample;
        if (!sample || lastTrendSample.current.get(node.id) === sample.id) continue;
        lastTrendSample.current.set(node.id, sample.id);
        const gpu = sample.gpu_utilization_percent;
        if (typeof gpu !== "number" || !Number.isFinite(gpu)) continue;
        next[node.id] = [...(current[node.id] ?? []), gpu].slice(-12);
        changed = true;
      }
      return changed ? next : current;
    });
  }, [fleet.snapshot]);

  const summary = useMemo(
    () => fleet.snapshot ? summarizeFleet(fleet.snapshot, fleet.now) : undefined,
    [fleet.now, fleet.snapshot],
  );
  const announcementMessage = summary
    ? `Fleet status: ${summary.live} live, ${summary.delayed} delayed, ${summary.stale} stale, ${summary.offline} offline; ${countLabel(summary.installedRecipes, "installed recipe")}; ${countLabel(summary.loadedRecipes, "loaded recipe")}; ${countLabel(summary.warnings, "warning")}.`
    : "";
  const announcement = usePoliteAnnouncement(announcementMessage);
  const selectedNode = fleet.snapshot?.nodes.find(node => node.id === selectedNodeId);
  const connection = connectionPresentation(fleet.connection);

  function selectNode(nodeId: string) {
    if (document.activeElement instanceof HTMLElement) detailTrigger.current = document.activeElement;
    setSelectedNodeId(nodeId);
  }

  function closeDetail() {
    setSelectedNodeId(undefined);
    queueMicrotask(() => detailTrigger.current?.focus());
  }

  function closeOnboarding() {
    setOnboarding(false);
    queueMicrotask(() => onboardingTrigger.current?.focus());
  }

  function changeView(next: FleetViewMode) {
    setViewMode(next);
    try { localStorage.setItem(FLEET_VIEW_STORAGE_KEY, next); } catch { /* Preference persistence is optional. */ }
  }

  return <div className="fleet-page">
    <header className="fleet-hero">
      <div>
        <p className="fleet-kicker">Reactive control plane</p>
        <h1>Fleet</h1>
        <p className="fleet-introduction">A live view of PostgreSQL-registered nodes, their capacity, and what is actually installed and running.</p>
      </div>
      <div className="fleet-hero-actions">
        <button type="button" className="button" aria-label="Add Spark" onClick={event => { onboardingTrigger.current = event.currentTarget; setOnboarding(true); }}>+ Add Spark</button>
        <div className="connection-state" aria-label="Fleet stream state">
          <StatusPill tone={connection.tone}>{connection.label}</StatusPill>
          {fleet.snapshot && <small>Event {fleet.snapshot.event_cursor} · authority {fleet.snapshot.authority_revision.slice(0, 8)}</small>}
        </div>
      </div>
    </header>
    {onboarding && <SparkOnboarding api={api} onBusyChange={onBusyChange} onClose={closeOnboarding}/>}

    <p className="sr-only" aria-live="polite" aria-atomic="true">{announcement}</p>

    {summary && <section className="fleet-summary" aria-label="Fleet summary">
      <div className="fleet-capacity">
        <span>Live unified memory</span>
        <strong>{summary.unifiedCapacity === "partial" ? `${formatBytes(summary.unifiedAvailableBytes)} known` : formatBytes(summary.unifiedAvailableBytes)}</strong>
        <small>{summary.unifiedCapacity === "known"
          ? `All ${countLabel(summary.live, "live node")} reporting`
          : summary.unifiedCapacity === "partial"
            ? `Partial · ${summary.unifiedReportingNodes} of ${summary.live} live nodes reporting`
            : "No live node reports both host and GPU free memory"}</small>
        {summary.unifiedAvailableBytes !== null && summary.unifiedTotalBytes !== null && <Meter
          label="Memory used"
          max={summary.unifiedTotalBytes}
          value={summary.unifiedTotalBytes - summary.unifiedAvailableBytes}
          valueLabel={`${formatBytes(summary.unifiedAvailableBytes)} available of ${formatBytes(summary.unifiedTotalBytes)}`}
        />}
      </div>
      <div className="fleet-health-summary">
        <div className="fleet-health-strip" role="img" aria-label={`${summary.live} live, ${summary.delayed} delayed, ${summary.stale} stale, ${summary.offline} offline`}>
          {(["live", "delayed", "stale", "offline"] as const).map(state => summary[state] > 0 && <span key={state} className={`health-${state}`} style={{flexGrow: summary[state]}} aria-hidden="true"/>)}
        </div>
        <dl className="fleet-state-counts">
          {(["live", "delayed", "stale", "offline"] as const).map(state => <div key={state} className={`summary-${state}`}>
            <dt>{state.charAt(0).toUpperCase() + state.slice(1)}</dt>
            <dd>{summary[state]}</dd>
          </div>)}
        </dl>
      </div>
      <div className="fleet-activity">
        <strong>{countLabel(summary.loadedRecipes, "loaded recipe")}</strong>
        <span>{countLabel(summary.installedRecipes, "installed recipe")}</span>
        <span>{countLabel(summary.warnings, "active warning")}</span>
        <small>{countLabel(summary.total, "registered node")}</small>
      </div>
    </section>}

    {fleet.loading && !fleet.snapshot && <section className="fleet-loading" aria-label="Loading Fleet" role="status">
      <span className="loading-orb" aria-hidden="true"/>
      <div><h3>Joining the Fleet stream</h3><p>Loading the latest registered Fleet projection and node telemetry…</p></div>
    </section>}

    {fleet.error && !fleet.snapshot && <section className="fleet-error" role="alert">
      <p className="fleet-kicker">Connection problem</p>
      <h3>Fleet unavailable</h3>
      <p>{fleet.error}</p>
      <button type="button" onClick={fleet.retry}>Retry Fleet</button>
    </section>}

    {fleet.snapshot && fleet.snapshot.nodes.length === 0 && <section className="fleet-empty">
      <p className="fleet-kicker">PostgreSQL registration projection is ready</p>
      <h3>No registered Fleet nodes</h3>
      <p>Add the first managed node through the onboarding workflow. A silent or offline node would still appear here.</p>
      <button type="button" className="button" onClick={event => { onboardingTrigger.current = event.currentTarget; setOnboarding(true); }}>Add your first Spark</button>
    </section>}

    {fleet.snapshot && fleet.snapshot.nodes.length > 0 && <>
      <div className="fleet-view-toolbar">
        <div><strong>Fleet view</strong><span>Choose the level of detail that suits this task.</span></div>
        <div className="fleet-view-switcher" role="group" aria-label="Fleet view">
          {FLEET_VIEWS.map(option => <button key={option.value} type="button" aria-pressed={viewMode === option.value} onClick={() => changeView(option.value)}><ViewIcon kind={option.icon}/><span>{option.label}</span></button>)}
        </div>
      </div>
      <div className={`fleet-workspace fleet-view-${viewMode}${selectedNode ? " has-detail" : ""}`}>
        {viewMode === "cards" && <section className="node-grid" aria-label="Fleet nodes cards">
          {fleet.snapshot.nodes.map(node => <NodeCard
            key={node.id}
            node={node}
            now={fleet.now}
            onSelect={() => selectNode(node.id)}
            selected={node.id === selectedNodeId}
            trend={gpuTrends[node.id]}
          />)}
        </section>}
        {viewMode === "compact" && <FleetCompactView nodes={fleet.snapshot.nodes} now={fleet.now} onSelect={selectNode} selectedNodeId={selectedNodeId}/>}
        {viewMode === "topology" && <FleetTopologyView nodes={fleet.snapshot.nodes} now={fleet.now} onSelect={selectNode} selectedNodeId={selectedNodeId}/>}
        {selectedNode && <NodeDetail api={api} node={selectedNode} now={fleet.now} onClose={closeDetail}/>}
      </div>
    </>}
  </div>;
}
