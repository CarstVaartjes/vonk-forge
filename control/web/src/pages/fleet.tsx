import {useEffect, useMemo, useRef, useState} from "react";
import type {KeyboardEvent as ReactKeyboardEvent} from "react";
import type {ControlApi, EnrollmentGrantResponse, TelemetryHistory, VisualFleetNode} from "../api/types";
import {CopyButton} from "../components/copy-button";
import {AgentUpgradeDialog} from "../components/agent-upgrade-dialog";
import {FleetCompactView, FleetTopologyView} from "../components/fleet-views";
import {FleetOperatingBoard} from "../components/fleet-operating-board";
import {NodeCard} from "../components/node-card";
import {NodeDetail} from "../components/node-detail";
import {NodeProfileDialog} from "../components/node-profile-dialog";
import {StatusPill} from "../components/status-pill";
import {useFleetStream} from "../hooks/use-fleet-stream";
import {usePoliteAnnouncement} from "../hooks/use-polite-announcement";
import {formatBytes, nodeDisplayName, nodeOperationalState, nodeSecondaryName, nodeWarningsAt, summarizeFleet} from "../lib/fleet";

export const ENROLLMENT_GRANT_TTL_SECONDS = 900;
export const FLEET_VIEW_STORAGE_KEY = "vonk-forge:fleet-view";
export type FleetViewMode = "detailed" | "compact" | "topology";
type FleetHealthFilter = ReturnType<typeof nodeOperationalState>;
type FleetSort = "attention" | "name";
type CardHistory = {error: string; history?: TelemetryHistory; loading: boolean};
type CardTrendRange = "1h" | "24h" | "7d" | "31d";

const CARD_TREND_RANGES: Record<CardTrendRange, {hours: number; label: string; maximumPoints: number; resolution: "minute" | "fifteen-minute"}> = {
  "1h": {hours: 1, label: "1h", maximumPoints: 60, resolution: "minute"},
  "24h": {hours: 24, label: "24h", maximumPoints: 1_440, resolution: "minute"},
  "7d": {hours: 24 * 7, label: "7d", maximumPoints: 672, resolution: "fifteen-minute"},
  "31d": {hours: 24 * 31, label: "31d", maximumPoints: 2_976, resolution: "fifteen-minute"},
};

const HEALTH_STATES = ["live", "delayed", "stale", "offline"] as const satisfies readonly FleetHealthFilter[];
const FLEET_VIEWS: readonly {label: string; value: FleetViewMode; icon: "detailed" | "compact" | "topology"}[] = [
  {label: "Detailed", value: "detailed", icon: "detailed"},
  {label: "Compact", value: "compact", icon: "compact"},
  {label: "Topology", value: "topology", icon: "topology"},
];

function storedFleetView(): FleetViewMode {
  try {
    const value = localStorage.getItem(FLEET_VIEW_STORAGE_KEY);
    if (value === "compact" || value === "topology") return value;
    // `cards` was persisted by releases before this view was renamed Detailed.
    return "detailed";
  } catch {
    return "detailed";
  }
}

function ViewIcon({kind}: {kind: "detailed" | "compact" | "topology"}) {
  if (kind === "detailed") return <svg aria-hidden="true" viewBox="0 0 20 20"><rect x="2" y="2" width="7" height="7" rx="1"/><rect x="11" y="2" width="7" height="7" rx="1"/><rect x="2" y="11" width="7" height="7" rx="1"/><rect x="11" y="11" width="7" height="7" rx="1"/></svg>;
  if (kind === "compact") return <svg aria-hidden="true" viewBox="0 0 20 20"><path d="M3 5h14M3 10h14M3 15h14"/><circle cx="3" cy="5" r="1"/><circle cx="3" cy="10" r="1"/><circle cx="3" cy="15" r="1"/></svg>;
  return <svg aria-hidden="true" viewBox="0 0 20 20"><circle cx="10" cy="4" r="2"/><circle cx="4" cy="15" r="2"/><circle cx="16" cy="15" r="2"/><path d="M10 6v3M10 9 4 13M10 9l6 4"/></svg>;
}

function attentionRank(node: VisualFleetNode, now: Date): number {
  const state = nodeOperationalState(node, now);
  const stateRank: Record<FleetHealthFilter, number> = {offline: 4, stale: 3, delayed: 2, live: 0};
  const warnings = nodeWarningsAt(node, now);
  const errorCount = warnings.filter(warning => warning.severity === "error").length;
  const degradedWork = node.installed.filter(item => !item.complete).length + node.loaded.filter(item => !item.healthy).length;
  return stateRank[state] * 1_000 + errorCount * 100 + warnings.length * 10 + degradedWork;
}

function filterAndSortNodes(
  nodes: readonly VisualFleetNode[],
  now: Date,
  query: string,
  healthFilters: readonly FleetHealthFilter[],
  warningsOnly: boolean,
  sort: FleetSort,
): VisualFleetNode[] {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  return nodes
    .filter(node => {
      if (healthFilters.length > 0 && !healthFilters.includes(nodeOperationalState(node, now))) return false;
      if (warningsOnly && nodeOperationalState(node, now) === "live" && nodeWarningsAt(node, now).length === 0) return false;
      if (!normalizedQuery) return true;
      const friendlyNames = [nodeDisplayName(node), nodeSecondaryName(node)].filter(Boolean).join(" ").toLocaleLowerCase();
      return friendlyNames.includes(normalizedQuery);
    })
    .sort((left, right) => {
      if (sort === "attention") {
        const difference = attentionRank(right, now) - attentionRank(left, now);
        if (difference !== 0) return difference;
      }
      return nodeDisplayName(left).localeCompare(nodeDisplayName(right), undefined, {numeric: true, sensitivity: "base"});
    });
}

function countLabel(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

function bootstrapCommand(grant: EnrollmentGrantResponse): string {
  const address = grant.controller_address;
  const environment = address ? `VONK_CONTROLLER_ADDRESS=${address} ` : "";
  return `curl -fsSL ${grant.installer_url} | ${environment}sh -s -- --enroll`;
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

function SparkOnboarding({api, mode, nodeId, onBusyChange, onClose}: {api: ControlApi; mode: "new-node" | "re-enroll"; nodeId?: string; onBusyChange?(busy: boolean): void; onClose(): void}) {
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
      setGrant(mode === "re-enroll"
        ? await api.createReenrollmentGrant(nodeId, ENROLLMENT_GRANT_TTL_SECONDS)
        : await api.createEnrollmentGrant(ENROLLMENT_GRANT_TTL_SECONDS));
    } catch (value) {
      setError(value instanceof Error ? value.message : "The enrollment grant could not be created.");
    } finally {
      creatingGrant.current = false;
      setCreating(false);
    }
  }

  return <div className="library-dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) requestClose(); }}>
    <div ref={dialog} className="library-action-dialog onboarding-dialog" role="dialog" aria-modal="true" aria-labelledby="spark-onboarding-title" aria-busy={creating || undefined} onKeyDown={handleKeyDown}>
      <header><div><p className="fleet-kicker">Secure node enrollment</p><h3 id="spark-onboarding-title">{mode === "re-enroll" ? "Re-enroll Spark" : "Add Spark"}</h3><p className="dialog-subtitle">Issue a short-lived pairing authorization.</p></div><button ref={closeButton} type="button" className="icon-button" disabled={creating} onClick={requestClose} aria-label={`Close ${mode === "re-enroll" ? "Re-enroll Spark" : "Add Spark"}`}>×</button></header>
      <div className="library-action-dialog-body">
        {!grant && <>
          <ol className="onboarding-steps" aria-label="Spark onboarding steps"><li><span className="onboarding-step-number" aria-hidden="true">1</span><div><strong>Create grant</strong><span>Generate a one-time authorization here.</span></div></li><li><span className="onboarding-step-number" aria-hidden="true">2</span><div><strong>Run installer</strong><span>Run the public installer on the Spark.</span></div></li><li><span className="onboarding-step-number" aria-hidden="true">3</span><div><strong>Enter credentials</strong><span>Provide the values shown when prompted.</span></div></li></ol>
          <p className="onboarding-guidance">{mode === "re-enroll" ? <>This explicitly replaces the certificate for {nodeId ? <code>{nodeId}</code> : "the Spark's locally held node identity"}. The installer retires stale local credential pointers automatically and preserves the node identity.</> : <>Use a descriptive hostname on the Spark; Fleet uses it as the friendly fallback name. The Spark generates its immutable <code>spk_…</code> identity locally. The installer verifies the release before requesting sudo and pins the controller CA.</>}</p>
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
  const [editingNodeId, setEditingNodeId] = useState<string>();
  const [onboarding, setOnboarding] = useState(false);
  const [onboardingMode, setOnboardingMode] = useState<"new-node" | "re-enroll">("new-node");
  const [reenrollNodeId, setReenrollNodeId] = useState<string>();
  const [upgradeTarget, setUpgradeTarget] = useState<"fleet" | {id: string; name: string}>();
  const [viewMode, setViewMode] = useState<FleetViewMode>(storedFleetView);
  const [query, setQuery] = useState("");
  const [healthFilters, setHealthFilters] = useState<FleetHealthFilter[]>([]);
  const [warningsOnly, setWarningsOnly] = useState(false);
  const [sort, setSort] = useState<FleetSort>("attention");
  const [cardTrendRange, setCardTrendRange] = useState<CardTrendRange>("24h");
  const [cardHistories, setCardHistories] = useState<Record<string, CardHistory>>({});
  const detailTrigger = useRef<HTMLElement | null>(null);
  const discoverySearch = useRef<HTMLInputElement>(null);
  const onboardingTrigger = useRef<HTMLElement | null>(null);
  const editTrigger = useRef<HTMLElement | null>(null);
  const upgradeTrigger = useRef<HTMLElement | null>(null);
  const completedCardHistories = useRef(new Set<string>());

  const summary = useMemo(
    () => fleet.snapshot ? summarizeFleet(fleet.snapshot, fleet.now) : undefined,
    [fleet.now, fleet.snapshot],
  );
  const announcementMessage = summary
    ? `Fleet status: ${summary.live} live, ${summary.delayed} delayed, ${summary.stale} stale, ${summary.offline} offline; ${countLabel(summary.installedRecipes, "installed recipe")}; ${countLabel(summary.loadedRecipes, "loaded recipe")}; ${countLabel(summary.warnings, "warning")}.`
    : "";
  const announcement = usePoliteAnnouncement(announcementMessage);
  const visibleNodes = useMemo(
    () => filterAndSortNodes(fleet.snapshot?.nodes ?? [], fleet.now, query, healthFilters, warningsOnly, sort),
    [fleet.now, fleet.snapshot?.nodes, healthFilters, query, sort, warningsOnly],
  );
  const selectedNode = visibleNodes.find(node => node.id === selectedNodeId);
  const editingNode = fleet.snapshot?.nodes.find(node => node.id === editingNodeId);
  const connection = connectionPresentation(fleet.connection);
  const filtersActive = query.trim().length > 0 || healthFilters.length > 0 || warningsOnly;
  const visibleNodeKey = visibleNodes.map(node => node.id).join("|");

  useEffect(() => {
    if (viewMode !== "detailed") return;
    const selection = CARD_TREND_RANGES[cardTrendRange];
    const nodeIds = visibleNodeKey.split("|").filter(Boolean).filter(nodeId => !completedCardHistories.current.has(`${cardTrendRange}:${nodeId}`));
    if (nodeIds.length === 0) return;
    let active = true;
    const controllers = new Set<AbortController>();
    let nextIndex = 0;
    setCardHistories(current => {
      const next = {...current};
      for (const nodeId of nodeIds) next[`${cardTrendRange}:${nodeId}`] = {error: "", loading: true};
      return next;
    });

    async function worker() {
      while (active && nextIndex < nodeIds.length) {
        const nodeId = nodeIds[nextIndex++]!;
        const controller = new AbortController();
        controllers.add(controller);
        const end = new Date();
        const start = new Date(end.getTime() - selection.hours * 60 * 60 * 1_000);
        try {
          const history = await api.nodeTelemetryHistory(nodeId, start.toISOString(), end.toISOString(), selection.resolution, selection.maximumPoints, controller.signal);
          if (!active || controller.signal.aborted) continue;
          const historyKey = `${cardTrendRange}:${nodeId}`;
          completedCardHistories.current.add(historyKey);
          setCardHistories(current => ({...current, [historyKey]: {error: "", history, loading: false}}));
        } catch (value) {
          if (!active || controller.signal.aborted) continue;
          const historyKey = `${cardTrendRange}:${nodeId}`;
          completedCardHistories.current.add(historyKey);
          const message = value instanceof Error ? value.message : "Telemetry history is unavailable";
          setCardHistories(current => ({...current, [historyKey]: {error: message.slice(0, 256), loading: false}}));
        } finally {
          controllers.delete(controller);
        }
      }
    }
    void Promise.all(Array.from({length: Math.min(3, nodeIds.length)}, () => worker()));
    return () => {
      active = false;
      for (const controller of controllers) controller.abort();
      controllers.clear();
    };
  }, [api, cardTrendRange, viewMode, visibleNodeKey]);

  useEffect(() => {
    if (!selectedNodeId || selectedNode) return;
    const detailHadFocus = document.activeElement instanceof HTMLElement && Boolean(document.activeElement.closest(".node-detail"));
    setSelectedNodeId(undefined);
    detailTrigger.current = null;
    if (detailHadFocus) queueMicrotask(() => discoverySearch.current?.focus());
  }, [selectedNode, selectedNodeId]);

  function selectNode(nodeId: string) {
    if (document.activeElement instanceof HTMLElement) detailTrigger.current = document.activeElement;
    setSelectedNodeId(nodeId);
  }

  function closeDetail() {
    setSelectedNodeId(undefined);
    const trigger = detailTrigger.current;
    queueMicrotask(() => (trigger?.isConnected ? trigger : discoverySearch.current)?.focus());
  }

  function closeOnboarding() {
    setOnboarding(false);
    setOnboardingMode("new-node");
    setReenrollNodeId(undefined);
    queueMicrotask(() => onboardingTrigger.current?.focus());
  }

  function editNode(nodeId: string) {
    if (document.activeElement instanceof HTMLElement) editTrigger.current = document.activeElement;
    setEditingNodeId(nodeId);
  }

  function closeEditor() {
    setEditingNodeId(undefined);
    queueMicrotask(() => editTrigger.current?.focus());
  }

  function closeUpgrade() {
    setUpgradeTarget(undefined);
    queueMicrotask(() => upgradeTrigger.current?.focus());
  }

  function changeView(next: FleetViewMode) {
    setViewMode(next);
    try { localStorage.setItem(FLEET_VIEW_STORAGE_KEY, next); } catch { /* Preference persistence is optional. */ }
  }

  function toggleHealthFilter(state: FleetHealthFilter) {
    setHealthFilters(current => current.includes(state) ? current.filter(value => value !== state) : [...current, state]);
  }

  function clearDiscoveryFilters() {
    setQuery("");
    setHealthFilters([]);
    setWarningsOnly(false);
  }

  return <div className="fleet-page">
    <header className="fleet-command-header">
      <div className="fleet-command-title">
        <h1>Fleet</h1>
        <div className="connection-state" aria-label="Fleet stream state">
          <StatusPill tone={connection.tone}>{connection.label}</StatusPill>
          {fleet.snapshot && <small>e{fleet.snapshot.event_cursor} · {fleet.snapshot.authority_revision.slice(0, 8)}</small>}
        </div>
      </div>

      {summary && <section className="fleet-command-summary" aria-label="Fleet summary">
        <div className="fleet-state-counts" role="group" aria-label="Filter Fleet by health">
          {HEALTH_STATES.map(state => <button key={state} type="button" className={`summary-${state}`} aria-pressed={healthFilters.includes(state)} aria-label={`${healthFilters.includes(state) ? "Hide" : "Show"} ${state} nodes`} onClick={() => toggleHealthFilter(state)}>
            <span>{state.charAt(0).toUpperCase() + state.slice(1)}</span>
            <strong>{summary[state]}</strong>
          </button>)}
        </div>
        <div className="fleet-command-fact fleet-command-memory">
          <span>Unified free</span>
          <strong>{summary.unifiedCapacity === "partial" ? `${formatBytes(summary.unifiedAvailableBytes)} known` : formatBytes(summary.unifiedAvailableBytes)}</strong>
          <small className="sr-only">{summary.unifiedCapacity === "known"
            ? `All ${countLabel(summary.live, "live node")} reporting`
            : summary.unifiedCapacity === "partial"
              ? `Partial · ${summary.unifiedReportingNodes} of ${summary.live} live nodes reporting`
              : "No live node reports both host and GPU free memory"}</small>
        </div>
        <div className="fleet-command-fact"><span>Loaded</span><strong>{summary.loadedRecipes}</strong><small className="sr-only">{countLabel(summary.loadedRecipes, "loaded recipe")}</small></div>
        <div className="fleet-command-fact"><span>Installed</span><strong>{summary.installedRecipes}</strong><small className="sr-only">{countLabel(summary.installedRecipes, "installed recipe")}</small></div>
        <button type="button" className="fleet-command-warning" aria-label={countLabel(summary.warnings, "active warning")} aria-pressed={warningsOnly} onClick={() => setWarningsOnly(value => !value)}><span>Warnings</span><strong>{summary.warnings}</strong></button>
      </section>}

      <div className="fleet-command-actions">
        <a className="button" href="/library">Install model</a>
        {fleet.snapshot && fleet.snapshot.nodes.length > 0 && <button type="button" className="button secondary" onClick={event => { upgradeTrigger.current = event.currentTarget; setUpgradeTarget("fleet"); }}>Upgrade agents</button>}
        {fleet.snapshot && fleet.snapshot.nodes.length > 0 && <details className="fleet-controls-menu">
          <summary>Controls{filtersActive ? <span aria-label="Filters active">•</span> : null}</summary>
          <div className="fleet-controls-popover">
            <div className="fleet-controls-popover-heading">
              <strong>Fleet controls</strong>
              <button type="button" aria-label="Close Fleet controls" onClick={event => {
                const menu = event.currentTarget.closest("details");
                menu?.removeAttribute("open");
                menu?.querySelector<HTMLElement>("summary")?.focus();
              }}>Close</button>
            </div>
            <section className="fleet-controls-basic" aria-label="Secondary Fleet controls">
              <label className="fleet-sort"><span>Sort</span><select value={sort} onChange={event => setSort(event.currentTarget.value as FleetSort)}><option value="attention">Attention first</option><option value="name">Name A–Z</option></select></label>
            </section>
            <div className="fleet-view-toolbar">
              <div><strong>Fleet view</strong><span>Change density or inspect topology.</span></div>
              <div className="fleet-toolbar-controls">
                <label className="fleet-trend-range"><span>Card trends</span><select aria-label="Card trend range" value={cardTrendRange} onChange={event => setCardTrendRange(event.currentTarget.value as CardTrendRange)}>{(Object.keys(CARD_TREND_RANGES) as CardTrendRange[]).map(range => <option key={range} value={range}>{CARD_TREND_RANGES[range].label}</option>)}</select></label>
                <div className="fleet-view-switcher" role="group" aria-label="Fleet view">
                  {FLEET_VIEWS.map(option => <button key={option.value} type="button" aria-pressed={viewMode === option.value} onClick={() => changeView(option.value)}><ViewIcon kind={option.icon}/><span>{option.label}</span></button>)}
                </div>
              </div>
            </div>
          </div>
        </details>}
        <button type="button" className="button secondary fleet-reenroll-button" aria-label="Re-enroll Spark" onClick={event => { onboardingTrigger.current = event.currentTarget; setOnboardingMode("re-enroll"); setOnboarding(true); }}>Re-enroll</button>
        <button type="button" className="button fleet-add-button" aria-label="Add Spark" onClick={event => { onboardingTrigger.current = event.currentTarget; setOnboardingMode("new-node"); setOnboarding(true); }}>+ Spark</button>
      </div>
    </header>
    {onboarding && <SparkOnboarding api={api} mode={onboardingMode} nodeId={reenrollNodeId} onBusyChange={onBusyChange} onClose={closeOnboarding}/>}
    {editingNode && <NodeProfileDialog api={api} node={editingNode} onClose={closeEditor} onSaved={displayName => fleet.updateNodeProfile(editingNode.id, displayName)}/>}
    {upgradeTarget && <AgentUpgradeDialog api={api} node={upgradeTarget === "fleet" ? undefined : upgradeTarget} onBusyChange={onBusyChange} onClose={closeUpgrade}/>}

    <p className="sr-only" aria-live="polite" aria-atomic="true">{announcement}</p>

    {fleet.snapshot && fleet.snapshot.nodes.length > 0 && <section className="fleet-discovery fleet-discovery-primary" aria-label="Find and filter Sparks">
      <label className="fleet-search"><span>Find a Spark</span><input ref={discoverySearch} type="search" value={query} placeholder="Search friendly name or hostname" onChange={event => setQuery(event.currentTarget.value)}/></label>
      <fieldset className="fleet-health-filters"><legend>Health</legend><div>
        {HEALTH_STATES.map(state => <label key={state}><input type="checkbox" checked={healthFilters.includes(state)} onChange={() => toggleHealthFilter(state)}/><span>{state.charAt(0).toUpperCase() + state.slice(1)} <small>{summary?.[state] ?? 0}</small></span></label>)}
      </div></fieldset>
      <div className="fleet-discovery-footer"><span role="status">Showing {visibleNodes.length} of {fleet.snapshot.nodes.length} {fleet.snapshot.nodes.length === 1 ? "Spark" : "Sparks"}</span>{filtersActive && <button type="button" className="fleet-clear-filters" onClick={clearDiscoveryFilters}>Clear filters</button>}</div>
    </section>}

    {fleet.loading && !fleet.snapshot && <section className="fleet-loading" aria-label="Loading Fleet" role="status">
      <span className="loading-orb" aria-hidden="true"/>
      <div><h3>Joining the Fleet stream</h3><p>Loading the latest registered Fleet projection and node telemetry…</p></div>
    </section>}

    {fleet.error && !fleet.snapshot && <section className="fleet-error" role="alert">
      <h3>Fleet unavailable</h3>
      <p>{fleet.error} Check the controller connection, then try again.</p>
      <button type="button" onClick={fleet.retry}>Retry Fleet</button>
    </section>}

    {fleet.snapshot && fleet.snapshot.nodes.length === 0 && <section className="fleet-empty">
      <h3>No registered Fleet nodes</h3>
      <p>The controller is ready. Add the first managed Spark through onboarding; once registered, even a silent or offline Spark remains visible here.</p>
      <button type="button" className="button" onClick={event => { onboardingTrigger.current = event.currentTarget; setOnboarding(true); }}>Add your first Spark</button>
    </section>}

    {fleet.snapshot && fleet.snapshot.nodes.length > 0 && <>
      <FleetOperatingBoard api={api} nodes={fleet.snapshot.nodes} now={fleet.now} onManageNode={selectNode}/>
      <div className="fleet-roster-heading">
        <div><h2>Spark roster</h2><p>Inspect capacity, telemetry, and workload evidence for each managed Spark.</p></div>
        <span>{visibleNodes.length} of {fleet.snapshot.nodes.length}</span>
      </div>
      <div className={`fleet-workspace fleet-view-${viewMode}${selectedNode ? " has-detail" : ""}`}>
        {visibleNodes.length === 0 && <section className="fleet-filter-empty" aria-label="No matching Sparks"><h3>No Sparks match these filters</h3><p>Try another friendly name or include more health states.</p><button type="button" className="button secondary" aria-label="Clear Fleet filters from empty state" onClick={clearDiscoveryFilters}>Clear filters</button></section>}
        {visibleNodes.length > 0 && viewMode === "detailed" && <section className="node-grid" aria-label="Fleet nodes detailed">
          {visibleNodes.map(node => <NodeCard
            key={node.id}
            node={node}
            now={fleet.now}
            onEdit={() => editNode(node.id)}
            onSelect={() => selectNode(node.id)}
            selected={node.id === selectedNodeId}
            history={cardHistories[`${cardTrendRange}:${node.id}`]?.history}
            historyLabel={CARD_TREND_RANGES[cardTrendRange].label}
            historyLoading={cardHistories[`${cardTrendRange}:${node.id}`]?.loading}
            historyError={cardHistories[`${cardTrendRange}:${node.id}`]?.error}
          />)}
        </section>}
        {visibleNodes.length > 0 && viewMode === "compact" && <FleetCompactView nodes={visibleNodes} now={fleet.now} onSelect={selectNode} selectedNodeId={selectedNodeId}/>}
        {visibleNodes.length > 0 && viewMode === "topology" && <FleetTopologyView nodes={visibleNodes} now={fleet.now} onSelect={selectNode} selectedNodeId={selectedNodeId}/>}
        {selectedNode && <NodeDetail
          api={api}
          node={selectedNode}
          now={fleet.now}
          onClose={closeDetail}
          onLifecycleRefresh={fleet.refresh}
          onBusyChange={onBusyChange}
          onReenroll={event => {
            onboardingTrigger.current = event.currentTarget;
            setOnboardingMode("re-enroll");
            setReenrollNodeId(selectedNode.id);
            setOnboarding(true);
          }}
          onUpgrade={event => { upgradeTrigger.current = event.currentTarget; setUpgradeTarget({id: selectedNode.id, name: nodeDisplayName(selectedNode)}); }}
        />}
      </div>
    </>}
  </div>;
}
