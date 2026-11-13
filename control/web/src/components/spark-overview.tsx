import type { ControlApi, MetricRange, VisualFleetNode } from "../api/types";
import { OutcomeKind } from "../api/vocabulary.generated";
import {
  formatBytes,
  nodeCpuClock,
  nodeDisplayMemory,
  nodeGpuClock,
  nodeRecipeUpdates,
  nodeStatus,
  nodeSwap,
} from "../lib/fleet";
import type { HistoryPoint } from "../hooks/use-telemetry-history";
import { InstallationReconcile } from "./installation-reconcile";
import { SparkCharts } from "./spark-charts";
import { SparkGpuNow } from "./spark-gpu-now";
import { SparkMemory } from "./spark-memory";
import { StatusPill } from "./status-pill";

/** The Overview tab: the Spark's trends, its readings and what needs attention. */
export function SparkOverview({
  api,
  node,
  now,
  range,
  color,
  points,
}: {
  api: ControlApi;
  node: VisualFleetNode;
  now: Date;
  range: MetricRange;
  color: string;
  /** The last 30 minutes of the Spark's telemetry, kept by the page across tabs. */
  points: readonly HistoryPoint[];
}) {
  const cpu = nodeCpuClock(node);
  const status = nodeStatus(node, now);
  return (
    <>
      <div className="spark-panels">
        <SparkMemory node={node} points={points} now={now} />
        <SparkGpuNow node={node} points={points} now={now} color={color} />
      </div>
      <SparkCharts api={api} nodeId={node.id} range={range} color={color} />
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
      {status.reasons.length > 0 && (
        <ul className="spark-warnings" aria-label="Needs attention">
          {status.reasons.map((reason, index) => (
            <li key={index}>
              <StatusPill tone="warn">
                {status.status === "offline" ? "offline" : "attention"}
              </StatusPill>{" "}
              {reason}
            </li>
          ))}
        </ul>
      )}
      {nodeRecipeUpdates(node).length > 0 && (
        <ul className="spark-warnings" aria-label="Updates available">
          {nodeRecipeUpdates(node).map((update) => (
            <li key={update.runId}>
              <StatusPill tone="info">update available</StatusPill> {update.detail}
            </li>
          ))}
        </ul>
      )}
      {node.installed.some((item) => item.complete === false) && (
        <ul className="spark-warnings" aria-label="Incomplete installations">
          {node.installed
            .filter((item) => item.complete === false)
            .map((item) => (
              <li key={item.installation_id}>
                <StatusPill tone="warn">{item.group_state}</StatusPill> {item.title} ·{" "}
                {item.present_ranks?.length ?? OutcomeKind.UNKNOWN} of{" "}
                {item.expected_rank_count ?? OutcomeKind.UNKNOWN} ranks{" "}
                <InstallationReconcile api={api} installationId={item.installation_id} />
              </li>
            ))}
        </ul>
      )}
    </>
  );
}
