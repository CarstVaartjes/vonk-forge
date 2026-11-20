import "./spark.css";
import { useEffect, useState, type KeyboardEvent, type MouseEvent } from "react";
import type { ControlApi, MetricRange } from "../api/types";
import { FleetFeedback } from "../components/fleet-feedback";
import { RangeSwitch } from "../components/range-switch";
import { SparkLogs } from "../components/spark-logs";
import { SparkOverview } from "../components/spark-overview";
import { SparkSettings } from "../components/spark-settings";
import { StatusPill } from "../components/status-pill";
import { Time } from "../components/time";
import { sparkColor } from "../components/chart/series";
import { type LiveStatus, liveStatus } from "../hooks/fleet-stream-state";
import { useFleetStream } from "../hooks/use-fleet-stream";
import { useTelemetryHistory } from "../hooks/use-telemetry-history";
import {
  NOT_REPORTED_TEXT,
  liveSample,
  nodeDisplayName,
  nodeStatus,
  nodeStatusTone,
  uptimeText,
} from "../lib/fleet";

type Tab = "overview" | "settings" | "logs";
const TABS: Array<[Tab, string]> = [
  ["overview", "Overview"],
  ["settings", "Settings"],
  ["logs", "Logs"],
];

/** WAI-ARIA tabs: Left/Right wrap, Home/End jump; the arrowed tab is selected and focused. */
function nextTab(key: string, from: number): number | null {
  if (key === "Home") return 0;
  if (key === "End") return TABS.length - 1;
  if (key === "ArrowRight") return (from + 1) % TABS.length;
  if (key === "ArrowLeft") return (from - 1 + TABS.length) % TABS.length;
  return null;
}

/**
 * One Spark on its own page: breadcrumb, colour key, name and status, tabs, the range every chart
 * shares, and the facts that need no chart. The Spark is read from the fleet stream, so everything
 * shown is current; a Spark the fleet does not know says so instead of showing an empty page.
 */
export function SparkPage({
  api,
  id,
  onLive,
  onNavigate,
  onGone,
}: {
  api: ControlApi;
  id: string;
  onLive?(status: LiveStatus | null): void;
  onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void;
  onGone(): void;
}) {
  const fleet = useFleetStream(api);
  const live = liveStatus(fleet);
  useEffect(() => onLive?.(live), [onLive, live]);
  useEffect(() => () => onLive?.(null), [onLive]);
  const [tab, setTab] = useState<Tab>("overview");
  const [range, setRange] = useState<MetricRange>("24h");
  useEffect(() => setTab("overview"), [id]);

  function moveTab(event: KeyboardEvent<HTMLDivElement>) {
    const target = nextTab(
      event.key,
      TABS.findIndex(([key]) => key === tab),
    );
    if (target === null) return;
    event.preventDefault();
    const [key] = TABS[target]!;
    setTab(key);
    event.currentTarget.querySelector<HTMLElement>(`#spark-tab-${key}`)?.focus();
  }

  const nodes = fleet.snapshot?.nodes ?? [];
  const index = nodes.findIndex((candidate) => candidate.id === id);
  const node = nodes[index];
  const { points } = useTelemetryHistory({
    api,
    nodeId: id,
    telemetry: node?.telemetry,
    now: fleet.now,
  });
  const name = node ? nodeDisplayName(node) : id;
  const status = node ? nodeStatus(node, fleet.now).status : null;
  const uptime = node ? uptimeText(liveSample(node)?.uptime_seconds) : null;
  const agent = node?.agent_runtime?.semantic_version;

  return (
    <div className="fleet-page spark-page">
      <nav className="breadcrumb" aria-label="Breadcrumb">
        <a href="/overview" onClick={(event) => onNavigate(event, "/overview")}>
          Overview
        </a>
        <span aria-hidden="true"> / </span>
        <span aria-current="page">{name}</span>
      </nav>
      <FleetFeedback fleet={fleet} />
      {fleet.snapshot && !node && (
        <section className="fleet-empty" role="status">
          <h1>Spark not found</h1>
          <p>No enrolled Spark has this address. It may have been removed.</p>
          <a
            className="button"
            href="/overview"
            onClick={(event) => onNavigate(event, "/overview")}
          >
            Back to Overview
          </a>
        </section>
      )}
      {node && status && (
        <>
          <header className="spark-page-head">
            <span
              className="spark-key"
              style={{ background: sparkColor(index) }}
              aria-hidden="true"
            />
            <h1>{name}</h1>
            <StatusPill tone={nodeStatusTone(status)}>{status}</StatusPill>
            <span className="spark-seen">
              seen <Time value={node.connection.last_seen_at} />
            </span>
            <div className="spark-page-controls">
              <div
                className="segmented"
                role="tablist"
                aria-label="Spark sections"
                onKeyDown={moveTab}
              >
                {TABS.map(([key, label]) => (
                  <button
                    key={key}
                    type="button"
                    role="tab"
                    id={`spark-tab-${key}`}
                    aria-selected={tab === key}
                    tabIndex={tab === key ? 0 : -1}
                    aria-controls="spark-tab-panel"
                    onClick={() => setTab(key)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <RangeSwitch range={range} onChange={setRange} />
            </div>
          </header>
          <ul className="spark-chips" aria-label="Facts">
            <li>
              Uptime <strong>{uptime ?? NOT_REPORTED_TEXT}</strong>
            </li>
            <li>
              Agent <strong>{agent ?? NOT_REPORTED_TEXT}</strong>
            </li>
            <li>
              Host{" "}
              <strong>
                {node.hostname}
                {node.ip_address ? ` · ${node.ip_address}` : ""}
              </strong>
            </li>
          </ul>
          <div
            className="spark-panel"
            role="tabpanel"
            id="spark-tab-panel"
            aria-labelledby={`spark-tab-${tab}`}
          >
            {tab === "overview" && (
              <SparkOverview
                api={api}
                node={node}
                now={fleet.now}
                range={range}
                color={sparkColor(index)}
                nodes={nodes}
                attention={fleet.snapshot?.attention}
                attentionUnavailable={fleet.snapshot?.attention_unavailable === true}
                onNavigate={onNavigate}
                points={points}
              />
            )}
            {tab === "settings" && (
              <SparkSettings api={api} node={node} onChanged={fleet.retry} onRemoved={onGone} />
            )}
            {tab === "logs" && <SparkLogs api={api} id={id} />}
          </div>
        </>
      )}
    </div>
  );
}
