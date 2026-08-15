import {useEffect, useId, useRef, useState} from "react";
import type {ControlApi, TelemetryHistory, VisualFleetNode} from "../api/types";
import {formatBytes, installationGroupLabel, runGroupLabel} from "../lib/fleet";
import {Sparkline} from "./sparkline";
import {StatusPill} from "./status-pill";

type HistoryRange = "1h" | "6h" | "24h";

const HISTORY_RANGES: Record<HistoryRange, {hours: number; label: string; maximumPoints: number}> = {
  "1h": {hours: 1, label: "1 hour", maximumPoints: 360},
  "6h": {hours: 6, label: "6 hours", maximumPoints: 720},
  "24h": {hours: 24, label: "24 hours", maximumPoints: 1440},
};

function boundedError(value: unknown): string {
  const message = value instanceof Error ? value.message : "Telemetry history is unavailable";
  return message.length > 512 ? `${message.slice(0, 512)}…` : message;
}

export function NodeDetail({
  api,
  node,
  now,
  onClose,
}: {
  api: ControlApi;
  node: VisualFleetNode;
  now: Date;
  onClose(): void;
}) {
  const headingId = useId();
  const closeButton = useRef<HTMLButtonElement>(null);
  const historyEnd = useRef(new Date(now));
  const [range, setRange] = useState<HistoryRange>("1h");
  const [history, setHistory] = useState<TelemetryHistory>();
  const [historyError, setHistoryError] = useState("");
  const [historyLoading, setHistoryLoading] = useState(true);
  const [retryRevision, setRetryRevision] = useState(0);

  useEffect(() => { closeButton.current?.focus(); }, []);

  useEffect(() => {
    const controller = new AbortController();
    const selection = HISTORY_RANGES[range];
    const end = historyEnd.current;
    const start = new Date(end.getTime() - selection.hours * 60 * 60 * 1000);
    setHistoryLoading(true);
    setHistory(undefined);
    setHistoryError("");
    void api.nodeTelemetryHistory(
      node.id,
      start.toISOString(),
      end.toISOString(),
      selection.maximumPoints,
      controller.signal,
    ).then(result => {
      if (!controller.signal.aborted) setHistory(result);
    }).catch(value => {
      if (!controller.signal.aborted) {
        setHistory(undefined);
        setHistoryError(boundedError(value));
      }
    }).finally(() => {
      if (!controller.signal.aborted) setHistoryLoading(false);
    });
    return () => controller.abort();
  }, [api, node.id, range, retryRevision]);

  const points = history?.points ?? [];

  return <aside className="node-detail" role="complementary" aria-labelledby={headingId}>
    <header className="node-detail-heading">
      <div>
        <p className="node-eyebrow">Node detail</p>
        <h3 id={headingId}>{node.display_name} details</h3>
        <p>{node.hostname}</p>
      </div>
      <button ref={closeButton} type="button" className="secondary-button" aria-label={`Close ${node.display_name} details`} onClick={onClose}>Close</button>
    </header>

    <section aria-labelledby={`${headingId}-overview`}>
      <h4 id={`${headingId}-overview`}>Overview</h4>
      <dl className="detail-facts">
        <div><dt>Agent</dt><dd><StatusPill tone={node.connection.online_state === "online" ? "healthy" : "danger"}>{node.connection.online_state}</StatusPill> {node.connection.agent_state}</dd></div>
        <div><dt>Lifecycle</dt><dd>{node.lifecycle}</dd></div>
        <div><dt>Last agent presence</dt><dd>{node.connection.last_seen_at ?? "Not reported"}</dd></div>
        <div><dt>Latest telemetry</dt><dd>{node.telemetry?.sample.observed_at ?? "Not reported"}</dd></div>
        <div><dt>Inventory</dt><dd>{node.inventory?.observed_at ?? "Not reported"}</dd></div>
        <div><dt>Reservations</dt><dd>{formatBytes(node.reservations.unified_memory_bytes)} unified · {formatBytes(node.reservations.disk_bytes)} disk · {node.reservations.port_count} ports</dd></div>
      </dl>
    </section>

    <section aria-labelledby={`${headingId}-recipes`}>
      <h4 id={`${headingId}-recipes`}>Recipes</h4>
      <div className="detail-recipe-columns">
        <div><h5>Loaded now</h5>{node.loaded.length === 0 ? <p>None reported</p> : <ul>{node.loaded.map(run => <li key={`${run.run_id}:${run.rank}`}><strong>{run.title}</strong><span>{runGroupLabel(run)}</span></li>)}</ul>}</div>
        <div><h5>Installed</h5>{node.installed.length === 0 ? <p>None reported</p> : <ul>{node.installed.map(item => <li key={`${item.installation_id}:${item.rank}`}><strong>{item.title}</strong><span>{installationGroupLabel(item)}</span></li>)}</ul>}</div>
      </div>
    </section>

    <section aria-labelledby={`${headingId}-performance`}>
      <div className="performance-heading">
        <h4 id={`${headingId}-performance`}>Performance</h4>
        <div className="history-ranges" role="group" aria-label="Telemetry history range">
          {(Object.entries(HISTORY_RANGES) as [HistoryRange, typeof HISTORY_RANGES[HistoryRange]][]).map(([value, option]) => <button key={value} type="button" aria-pressed={range === value} onClick={() => setRange(value)}>{option.label}</button>)}
        </div>
      </div>
      {historyLoading && <p role="status">Loading bounded telemetry history…</p>}
      {historyError && <div className="history-error"><p role="alert">{historyError}</p><button type="button" onClick={() => setRetryRevision(value => value + 1)}>Retry history</button></div>}
      {history && <div className="history-grid">
        <Sparkline label={`${node.display_name} GPU utilization history`} values={points.map(point => point.gpu_utilization_percent)} formatValue={value => `${Math.round(value)}%`}/>
        <Sparkline label={`${node.display_name} available memory history`} values={points.map(point => point.memory_available_bytes)} formatValue={formatBytes}/>
        <Sparkline label={`${node.display_name} temperature history`} values={points.map(point => point.temperature_c)} formatValue={value => `${Number(value.toFixed(1))} °C`}/>
      </div>}
    </section>

    <section aria-labelledby={`${headingId}-events`}>
      <h4 id={`${headingId}-events`}>Events</h4>
      {node.warnings.length === 0 ? <p>No active Fleet warnings.</p> : <ul>{node.warnings.map((warning, index) => <li key={`${warning.code}:${index}`}><strong>{warning.severity}</strong> {warning.detail}</li>)}</ul>}
    </section>

    <details className="technical-details">
      <summary>Technical details</summary>
      <dl className="detail-facts">
        <div><dt>Node ID</dt><dd><code>{node.id}</code></dd></div>
        <div><dt>Certificate</dt><dd>{node.connection.certificate_state}</dd></div>
        <div><dt>Telemetry sample</dt><dd><code>{node.telemetry?.sample.id ?? "Not reported"}</code></dd></div>
        <div><dt>Boot ID</dt><dd><code>{node.telemetry?.sample.boot_id ?? "Not reported"}</code></dd></div>
        <div><dt>Inventory runtime</dt><dd>{node.inventory?.container_runtime_version ?? "Not reported"}</dd></div>
        <div><dt>NVIDIA driver</dt><dd>{node.inventory?.nvidia_driver_version ?? "Not reported"}</dd></div>
      </dl>
    </details>
  </aside>;
}
