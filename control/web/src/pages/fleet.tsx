import {useEffect, useMemo, useRef, useState} from "react";
import type {ControlApi, UpdateSkew} from "../api/types";
import {NodeCard} from "../components/node-card";
import {NodeDetail} from "../components/node-detail";
import {StatusPill} from "../components/status-pill";
import {useFleetStream} from "../hooks/use-fleet-stream";
import {usePoliteAnnouncement} from "../hooks/use-polite-announcement";
import {formatBytes, summarizeFleet} from "../lib/fleet";

const DISMISSED_SKEW_KEY = "vonk-forge.dismissed-update-skew";

function bounded(value: string, maximum = 256): string {
  return value.length > maximum ? `${value.slice(0, maximum)}…` : value;
}

function previouslyDismissed(digest: string): boolean {
  try {
    return localStorage.getItem(DISMISSED_SKEW_KEY) === digest;
  } catch {
    return false;
  }
}

function countLabel(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? "" : "s"}`;
}

function connectionPresentation(connection: ReturnType<typeof useFleetStream>["connection"]) {
  switch (connection) {
    case "live": return {label: "Live connection", tone: "healthy" as const};
    case "reconnecting": return {label: "Reconnecting", tone: "warning" as const};
    case "polling": return {label: "Polling fallback", tone: "warning" as const};
    case "connecting": return {label: "Connecting", tone: "neutral" as const};
  }
}

export function FleetPage({api}: {api: ControlApi}) {
  const fleet = useFleetStream(api);
  const [skew, setSkew] = useState<UpdateSkew>();
  const [dismissed, setDismissed] = useState("");
  const [selectedNodeId, setSelectedNodeId] = useState<string>();
  const detailTrigger = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (typeof api.updateSkew !== "function") return;
    let current = true;
    api.updateSkew().then(result => {
      if (!current) return;
      setSkew(result);
      setDismissed(previouslyDismissed(result.digest) ? result.digest : "");
    }).catch(() => {
      // Fleet visibility remains useful when the update authority is
      // temporarily unavailable; the dedicated Updates page reports it.
    });
    return () => { current = false; };
  }, [api]);

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

  function dismissUpdate() {
    if (!skew) return;
    try {
      localStorage.setItem(DISMISSED_SKEW_KEY, skew.digest);
    } catch {
      // Session-local dismissal still works if durable browser storage is denied.
    }
    setDismissed(skew.digest);
  }

  function selectNode(nodeId: string) {
    if (document.activeElement instanceof HTMLElement) detailTrigger.current = document.activeElement;
    setSelectedNodeId(nodeId);
  }

  function closeDetail() {
    setSelectedNodeId(undefined);
    queueMicrotask(() => detailTrigger.current?.focus());
  }

  const showUpdate = skew?.prompt_required === true && dismissed !== skew.digest;

  return <div className="fleet-page">
    <header className="fleet-hero">
      <div>
        <p className="fleet-kicker">Reactive control plane</p>
        <h2>Fleet</h2>
        <p className="fleet-introduction">A live view of repository-defined nodes, their capacity, and what is actually installed and running.</p>
      </div>
      <div className="connection-state" aria-label="Fleet stream state">
        <StatusPill tone={connection.tone}>{connection.label}</StatusPill>
        {fleet.snapshot && <small>Event {fleet.snapshot.event_cursor} · repository {fleet.snapshot.repository_commit.slice(0, 8)}</small>}
      </div>
    </header>

    <p className="sr-only" aria-live="polite" aria-atomic="true">{announcement}</p>

    {showUpdate && skew && <section className="update-notice" aria-label="GPU node update available">
      <h3>Vonk Forge update available for GPU nodes</h3>
      <p>The NAS is running {bounded(skew.target.platform_version)} at <code>{bounded(skew.target.build_digest)}</code>. Review and explicitly confirm the signed rollout; this notice never updates a GPU node by itself.</p>
      <p>Affected GPU nodes: {skew.nodes.filter(node => skew.affected_nodes.includes(node.node_id)).slice(0, 1024).map(node => `${bounded(node.display_name)} (${bounded(node.node_id)})`).join(", ") || "none"}.</p>
      {skew.offline_pending.length > 0 && <p>Offline pending: {skew.offline_pending.map(bounded).join(", ")}.</p>}
      <p className="update-actions"><a href="/updates">Review platform update</a><button type="button" onClick={dismissUpdate}>Dismiss this exact update notice</button></p>
    </section>}

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
        <small>{countLabel(summary.total, "repository node")}</small>
      </div>
    </section>}

    {fleet.loading && !fleet.snapshot && <section className="fleet-loading" aria-label="Loading Fleet" role="status">
      <span className="loading-orb" aria-hidden="true"/>
      <div><h3>Joining the Fleet stream</h3><p>Loading the latest repository projection and node telemetry…</p></div>
    </section>}

    {fleet.error && !fleet.snapshot && <section className="fleet-error" role="alert">
      <p className="fleet-kicker">Connection problem</p>
      <h3>Fleet unavailable</h3>
      <p>{fleet.error}</p>
      <button type="button" onClick={fleet.retry}>Retry Fleet</button>
    </section>}

    {fleet.snapshot && fleet.snapshot.nodes.length === 0 && <section className="fleet-empty">
      <p className="fleet-kicker">Repository projection is ready</p>
      <h3>No nodes in the repository Fleet</h3>
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
