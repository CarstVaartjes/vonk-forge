import type { MouseEvent } from "react";
import type { ControlApi, MetricRange, VisualFleetNode, VisualFleetSnapshot } from "../api/types";
import { formatBytes, nodeCpuClock, nodeDisplayMemory, nodeGpuClock, nodeSwap } from "../lib/fleet";
import { useTelemetryHistory } from "../hooks/use-telemetry-history";
import { nodeHealth } from "../lib/node-health";
import { HealthList } from "./health-list";
import { SparkCharts } from "./spark-charts";
import { SparkGpuNow } from "./spark-gpu-now";
import { SparkMemory } from "./spark-memory";
import { SensorStrip } from "./sensor-strip";
import { SparkServing } from "./spark-serving";
import { SparkStorage } from "./spark-storage";
import { StatusPill } from "./status-pill";

/** The Overview tab: the Spark's trends, its readings and what needs attention. */
export function SparkOverview({
  api,
  node,
  now,
  range,
  color,
  nodes,
  attention,
  attentionUnavailable,
  onNavigate,
}: {
  api: ControlApi;
  node: VisualFleetNode;
  now: Date;
  range: MetricRange;
  color: string;
  /** The fleet, for the names of the Sparks this one shares a workload with. */
  nodes: readonly VisualFleetNode[];
  attention: VisualFleetSnapshot["attention"];
  attentionUnavailable: boolean;
  onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void;
}) {
  const cpu = nodeCpuClock(node);
  const { points } = useTelemetryHistory({ api, nodeId: node.id, telemetry: node.telemetry, now });
  return (
    <>
      <div className="spark-panels">
        <SparkMemory node={node} points={points} now={now} />
        <SparkGpuNow node={node} points={points} now={now} color={color} />
      </div>
      <SparkCharts api={api} nodeId={node.id} range={range} color={color} />
      <SensorStrip node={node} points={points} now={now} color={color} />
      <div className="spark-cards">
        <SparkServing node={node} nodes={nodes} onNavigate={onNavigate} />
        <SparkStorage node={node} />
        <section className="card spark-panel-card" aria-labelledby={`health-${node.id}`}>
          <header>
            <h2 id={`health-${node.id}`}>Health</h2>
          </header>
          <HealthList api={api} rows={nodeHealth(node, now, attention, attentionUnavailable)} />
        </section>
      </div>
      <dl className="spark-facts">
        <div>
          <dt>Display memory</dt>
          <dd>{nodeDisplayMemory(node)}</dd>
        </div>
        {cpu && (
          <div>
            <dt>CPU clock</dt>
            <dd>
              {cpu.clock}
              {cpu.temperature ? ` · ${cpu.temperature}` : ""}
              {cpu.lowClock && (
                <>
                  {" "}
                  <StatusPill tone="warn">low under load</StatusPill>
                </>
              )}
            </dd>
          </div>
        )}
        <div>
          <dt>GPU clock</dt>
          <dd>{nodeGpuClock(node)}</dd>
        </div>
        <div>
          <dt>Swap</dt>
          <dd>{nodeSwap(node)}</dd>
        </div>
        <div>
          <dt>Recipes</dt>
          <dd>
            {node.loaded.filter((item) => item.healthy === true).length}
            {node.loaded.some((item) => item.healthy == null) ? " confirmed" : ""} running ·{" "}
            {node.installed.filter((item) => item.complete === true).length}
            {node.installed.some((item) => item.complete == null) ? " confirmed" : ""} installed
            {(node.loaded.some((item) => item.healthy == null) ||
              node.installed.some((item) => item.complete == null)) && (
              <> · some workload state unknown</>
            )}
          </dd>
        </div>
      </dl>
      {node.loaded.some((item) => item.load_swap_held_bytes != null) && (
        <ul className="spark-run-swap" aria-label="Swap held while loading">
          {node.loaded
            .filter((item) => item.load_swap_held_bytes != null)
            .map((item) => (
              <li key={`${item.run_id}:${item.rank}`}>
                <strong>{item.title}</strong> Swap held while loading:{" "}
                {formatBytes(item.load_swap_held_bytes)}
              </li>
            ))}
        </ul>
      )}
      {node.loaded.some((item) => Object.keys(item.option_choices ?? {}).length > 0) && (
        <ul className="spark-run-options" aria-label="Active recipe options">
          {node.loaded
            .filter((item) => Object.keys(item.option_choices ?? {}).length > 0)
            .map((item) => (
              <li key={`${item.run_id}:${item.rank}`}>
                <strong>{item.title}</strong>{" "}
                {Object.entries(item.option_choices ?? {}).map(([name, value]) => (
                  <StatusPill key={name} tone="neutral">
                    {name}: {value}
                  </StatusPill>
                ))}
              </li>
            ))}
        </ul>
      )}
    </>
  );
}
