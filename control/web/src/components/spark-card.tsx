import "./spark-card.css";
import type { ControlApi, VisualFleetNode } from "../api/types";
import { HISTORY_SECONDS, useTelemetryHistory } from "../hooks/use-telemetry-history";
import { GPU_READINGS as READINGS } from "../lib/gpu-readings";
import {
  halfUp,
  liveSample,
  nodeDisplayName,
  nodeMemorySplit,
  nodeSecondaryName,
  nodeStatus,
  nodeStatusTone,
  nodeVitals,
  uptimeText,
} from "../lib/fleet";
import { sparkPath } from "../lib/spark-route";
import { sparkColor } from "./chart/series";
import { Sparkline } from "./chart/sparkline";
import { MemoryIngot } from "./memory-ingot";
import { StatusPill } from "./status-pill";

const NOT_REPORTED = "Not reported";

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
  const { points } = useTelemetryHistory({
    api,
    nodeId: node.id,
    telemetry: node.telemetry,
    now,
  });
  const end = now.getTime() / 1000;
  const sample = liveSample(node);
  return (
    <div className="spark-traces">
      {READINGS.map((reading) => {
        const value = sample ? reading.now(sample) : null;
        const shown =
          value === null
            ? null
            : "digits" in reading
              ? value.toFixed(reading.digits)
              : String(halfUp(value));
        const fixed = "fixedRange" in reading;
        return (
          <div className="spark-trace" key={reading.title}>
            <div className="spark-trace-head">
              <span>{reading.title}</span>
              <span className="spark-trace-value">
                {shown === null ? (
                  NOT_REPORTED
                ) : (
                  <>
                    {shown} <small>{reading.unit}</small>
                  </>
                )}
              </span>
            </div>
            <Sparkline
              points={points.map((point) => ({ t: point.t, v: reading.past(point) }))}
              start={end - HISTORY_SECONDS}
              end={end}
              min={fixed ? 0 : undefined}
              max={fixed ? 100 : undefined}
              color={color}
              label={`${name} ${reading.title}, last 30 minutes`}
            />
          </div>
        );
      })}
    </div>
  );
}

/**
 * One Spark: its colour key and name, status, unified memory and four live traces. `index` is the Spark's
 * place in the snapshot's node order and fixes its colour; `rank` shows when the workload spans Sparks. An offline Spark draws no traces. Readings the Spark
 * does not report say "Not reported"; the card never fills them in.
 */
export function SparkCard({
  api,
  node,
  now,
  index,
  rank,
  onOpen,
}: {
  api: Pick<ControlApi, "metricsSeries">;
  node: VisualFleetNode;
  now: Date;
  index: number;
  /** The Spark's rank when it is one of several carrying a single workload. */
  rank?: number;
  onOpen?(id: string): void;
}) {
  const name = nodeDisplayName(node);
  const { status, reasons } = nodeStatus(node, now);
  const color = sparkColor(index);
  const uptime = uptimeText(liveSample(node)?.uptime_seconds);
  const agent = node.agent_runtime?.semantic_version;
  const vitals = nodeVitals(node);
  return (
    <section className="spark-card" aria-label={name}>
      <header>
        <span className="spark-key" style={{ background: color }} aria-hidden="true" />
        <h3>
          <a
            href={sparkPath(node.id)}
            onClick={(event) => {
              if (!onOpen) return;
              event.preventDefault();
              onOpen(node.id);
            }}
          >
            {name}
          </a>
        </h3>
        {rank !== undefined && <span className="spark-rank">rank {rank}</span>}
        <span className="spark-uptime">{uptime ? `up ${uptime}` : `Uptime: ${NOT_REPORTED}`}</span>
      </header>
      <p className="spark-sub">
        <StatusPill tone={nodeStatusTone(status)}>{status}</StatusPill>
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
