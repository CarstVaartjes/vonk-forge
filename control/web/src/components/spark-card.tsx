import "./spark-card.css";
import type { ControlApi, VisualFleetNode } from "../api/types";
import {
  HISTORY_SECONDS,
  useTelemetryHistory,
  type HistoryPoint,
} from "../hooks/use-telemetry-history";
import {
  halfUp,
  nodeDisplayName,
  nodeMemorySplit,
  nodeSecondaryName,
  nodeStatus,
  nodeStatusTone,
  nodeVitals,
  uptimeText,
} from "../lib/fleet";
import { relativeTime } from "../lib/time";
import { Sparkline } from "./chart/sparkline";
import { MemoryIngot } from "./memory-ingot";
import { StatusPill } from "./status-pill";

const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];
const NOT_REPORTED = "Not reported";

type Reading = {
  key: "utilization" | "temperature" | "power" | "clockMhz";
  title: string;
  unit: string;
};
const READINGS: Reading[] = [
  { key: "utilization", title: "GPU", unit: "%" },
  { key: "temperature", title: "GPU temperature", unit: "°C" },
  { key: "power", title: "GPU power", unit: "W" },
  { key: "clockMhz", title: "GPU clock", unit: "MHz" },
];

function formatReading(key: Reading["key"], value: number | null): string | null {
  if (value === null) return null;
  return key === "power" ? value.toFixed(1) : String(halfUp(value));
}

/** The four live traces of an online Spark; the readings as the Spark reported them, the last 30 minutes behind each. */
function Traces({
  api,
  node,
  now,
  color,
  name,
}: {
  api: Pick<ControlApi, "metricsSeries">;
  node: VisualFleetNode;
  now: Date;
  color: string;
  name: string;
}) {
  const { points, live } = useTelemetryHistory({
    api,
    nodeId: node.id,
    telemetry: node.telemetry,
    now,
  });
  const end = now.getTime() / 1000;
  const latest = points.at(-1);
  return (
    <div className="spark-traces">
      {READINGS.map(({ key, title, unit }) => {
        const value = live && latest ? formatReading(key, latest[key]) : null;
        const series = points.map((point: HistoryPoint) => ({ t: point.t, v: point[key] }));
        return (
          <div className="spark-trace" key={key}>
            <div className="spark-trace-head">
              <span>{title}</span>
              {value === null ? (
                <span className="spark-trace-value">{NOT_REPORTED}</span>
              ) : (
                <span className="spark-trace-value">
                  {value} <small>{unit}</small>
                </span>
              )}
            </div>
            <Sparkline
              points={series}
              start={end - HISTORY_SECONDS}
              end={end}
              min={key === "utilization" ? 0 : undefined}
              max={key === "utilization" ? 100 : undefined}
              color={color}
              label={`${name} ${title}, last 30 minutes`}
            />
          </div>
        );
      })}
    </div>
  );
}

/**
 * One Spark: its colour key and name, status, unified memory and four live traces. `index` is the Spark's
 * place in enrolment order and fixes its colour. An offline Spark draws no traces. Readings the Spark
 * does not report say "Not reported"; the card never fills them in.
 */
export function SparkCard({
  api,
  node,
  now,
  index,
  onOpen,
}: {
  api: Pick<ControlApi, "metricsSeries">;
  node: VisualFleetNode;
  now: Date;
  index: number;
  onOpen?(id: string): void;
}) {
  const name = nodeDisplayName(node);
  const { status, reasons } = nodeStatus(node, now);
  const color = SERIES[index % SERIES.length];
  const run = node.loaded.find((item) => (item.member_node_ids?.length ?? 0) > 1);
  const uptime = uptimeText(node.telemetry?.sample.uptime_seconds);
  const seen = node.connection.last_seen_at
    ? relativeTime(node.connection.last_seen_at, now)
    : null;
  const agent = node.agent_runtime?.semantic_version;
  const vitals = nodeVitals(node);
  return (
    <section className="spark-card" aria-label={name}>
      <header>
        <span className="spark-key" style={{ background: color }} aria-hidden="true" />
        <h3>
          <a
            href={`?spark=${encodeURIComponent(node.id)}`}
            onClick={(event) => {
              if (!onOpen) return;
              event.preventDefault();
              onOpen(node.id);
            }}
          >
            {name}
          </a>
        </h3>
        {run && <span className="spark-rank">rank {String(run.rank)}</span>}
        <span className="spark-uptime">{uptime ? `up ${uptime}` : `Uptime: ${NOT_REPORTED}`}</span>
      </header>
      <p className="spark-sub">
        <StatusPill tone={nodeStatusTone(status)}>{status}</StatusPill>
        {seen && <span>seen {seen}</span>}
        {agent && <span>agent {agent}</span>}
        {nodeSecondaryName(node) && <span>{nodeSecondaryName(node)}</span>}
      </p>
      {reasons.length > 0 && (
        <p className="spark-reason" title={reasons.join("\n")}>
          {reasons[0]}
          {reasons.length > 1 ? ` +${reasons.length - 1} more` : ""}
        </p>
      )}
      <div className="spark-memory">
        <span>Unified memory</span>
        <strong>{vitals.memory.text}</strong>
      </div>
      <MemoryIngot label={`${name} unified memory`} memory={nodeMemorySplit(node)} />
      {node.connection.online_state === "online" && (
        <Traces api={api} node={node} now={now} color={color} name={name} />
      )}
      <div className="spark-quiet">
        <strong>{vitals.disk.text}</strong>
        <span>Disk</span>
      </div>
    </section>
  );
}
