import {useMemo, useRef, useState} from "react";
import type {ControlApi, EnrollmentGrantResponse} from "../api/types";
import {NodeCard} from "../components/node-card";
import {NodeDetail} from "../components/node-detail";
import {StatusPill} from "../components/status-pill";
import {useFleetStream} from "../hooks/use-fleet-stream";
import {usePoliteAnnouncement} from "../hooks/use-polite-announcement";
import {formatBytes, summarizeFleet} from "../lib/fleet";

export const ENROLLMENT_GRANT_TTL_SECONDS = 900;

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

function SparkOnboarding({api, onClose}: {api: ControlApi; onClose(): void}) {
  const [grant, setGrant] = useState<EnrollmentGrantResponse>();
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);

  async function createGrant() {
    setCreating(true);
    setError("");
    try {
      setGrant(await api.createEnrollmentGrant(ENROLLMENT_GRANT_TTL_SECONDS));
    } catch (value) {
      setError(value instanceof Error ? value.message : "The enrollment grant could not be created.");
    } finally {
      setCreating(false);
    }
  }

  return <div className="library-dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="library-action-dialog onboarding-dialog" role="dialog" aria-modal="true" aria-labelledby="spark-onboarding-title">
      <header><div><p className="fleet-kicker">Secure node enrollment</p><h3 id="spark-onboarding-title">Add Spark</h3><p className="dialog-subtitle">Issue a short-lived pairing authorization.</p></div><button type="button" className="icon-button" onClick={onClose} aria-label="Close Add Spark">×</button></header>
      <div className="library-action-dialog-body">
        {!grant && <>
          <ol className="onboarding-steps" aria-label="Spark onboarding steps"><li><strong>Create</strong><span>Generate a one-time grant here.</span></li><li><strong>Run</strong><span>Run the public installer on the Spark.</span></li><li><strong>Enter</strong><span>Provide the values shown here when prompted.</span></li></ol>
          <p className="onboarding-guidance">The Spark generates its immutable <code>spk_…</code> identity locally. The installer verifies the release before requesting sudo and pins the controller CA.</p>
          {error && <p role="alert" className="dialog-error">{error}</p>}
        </>}
        {grant && <>
          <div className="grant-success"><span className="success-mark" aria-hidden="true">✓</span><div><strong>One-time command ready</strong><span>Expires {grant.expires_at}. It will not be shown again after closing.</span></div></div>
          <p>Run this command on the Spark. Enter the enrollment URL, CA fingerprint, and one-time token below when prompted. The installer pins the controller CA and configures the NAS LAN route automatically.</p>
          <code className="onboarding-command">{bootstrapCommand(grant)}</code>
          <dl className="grant-facts"><div><dt>One-time token</dt><dd><code>{grant.token}</code></dd></div><div><dt>Controller</dt><dd>{grant.controller_endpoint}</dd></div><div><dt>Enrollment</dt><dd>{grant.enrollment_endpoint}</dd></div>{grant.controller_address && <div><dt>NAS LAN address</dt><dd><code>{grant.controller_address}</code></dd></div>}<div><dt>CA fingerprint</dt><dd><code>{grant.ca_fingerprint}</code></dd></div></dl>
        </>}
      </div>
      <footer>{grant ? <button type="button" className="button" onClick={onClose}>Done</button> : <><button type="button" className="button secondary" onClick={onClose}>Cancel</button><button type="button" className="button" disabled={creating} onClick={() => void createGrant()}>{creating ? "Creating…" : "Create one-time enrollment command"}</button></>}</footer>
    </div>
  </div>;
}


export function FleetPage({api}: {api: ControlApi}) {
  const fleet = useFleetStream(api);
  const [selectedNodeId, setSelectedNodeId] = useState<string>();
  const [onboarding, setOnboarding] = useState(false);
  const detailTrigger = useRef<HTMLElement | null>(null);

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

  return <div className="fleet-page">
    <header className="fleet-hero">
      <div>
        <p className="fleet-kicker">Reactive control plane</p>
        <h2>Fleet</h2>
        <p className="fleet-introduction">A live view of PostgreSQL-registered nodes, their capacity, and what is actually installed and running.</p>
      </div>
      <div className="fleet-hero-actions">
        <button type="button" className="button" aria-label="Add Spark" onClick={() => setOnboarding(true)}>+ Add Spark</button>
        <div className="connection-state" aria-label="Fleet stream state">
          <StatusPill tone={connection.tone}>{connection.label}</StatusPill>
          {fleet.snapshot && <small>Event {fleet.snapshot.event_cursor} · authority {fleet.snapshot.authority_revision.slice(0, 8)}</small>}
        </div>
      </div>
    </header>
    {onboarding && <SparkOnboarding api={api} onClose={() => setOnboarding(false)}/>}

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
      </div>
      <dl className="fleet-state-counts">
        {(["live", "delayed", "stale", "offline"] as const).map(state => <div key={state} className={`summary-${state}`}>
          <dt>{state.charAt(0).toUpperCase() + state.slice(1)}</dt>
          <dd>{summary[state]}</dd>
        </div>)}
      </dl>
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
    </section>}

    {fleet.snapshot && fleet.snapshot.nodes.length > 0 && <div className={`fleet-workspace${selectedNode ? " has-detail" : ""}`}>
      <section className="node-grid" aria-label="Fleet nodes">
        {fleet.snapshot.nodes.map(node => <NodeCard
          key={node.id}
          node={node}
          now={fleet.now}
          onSelect={() => selectNode(node.id)}
          selected={node.id === selectedNodeId}
        />)}
      </section>
      {selectedNode && <NodeDetail api={api} node={selectedNode} now={fleet.now} onClose={closeDetail}/>}
    </div>}
  </div>;
}
