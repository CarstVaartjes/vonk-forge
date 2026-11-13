import { useId } from "react";
import type { VisualFleetNode } from "../api/types";
import { HISTORY_SECONDS, type HistoryPoint } from "../hooks/use-telemetry-history";
import {
  GIB,
  MEMORY_WARN_FREE_BYTES,
  formatBytes,
  nodeDisplayName,
  nodeMemorySplit,
  nodeVitals,
} from "../lib/fleet";
import { StackedArea } from "./chart/stacked-area";
import { MemoryIngot } from "./memory-ingot";

const LAYERS = [
  { key: "models", color: "var(--mem-model)" },
  { key: "system", color: "var(--mem-system)" },
] as const;

/**
 * Unified memory on the Spark's page: the tall ingot with its legend, then Models and System over the
 * last 30 minutes from the live buffer, and what the two words mean. The split is as the Spark
 * reported it; a Spark that does not report it shows Used and Free and no history.
 */
export function SparkMemory({
  node,
  points,
  now,
}: {
  node: VisualFleetNode;
  points: readonly HistoryPoint[];
  now: Date;
}) {
  const id = useId();
  const name = nodeDisplayName(node);
  const memory = nodeMemorySplit(node);
  const end = now.getTime() / 1000;
  return (
    <section className="card spark-panel-card spark-memory-panel" aria-labelledby={id}>
      <header>
        <h2 id={id}>Unified memory</h2>
        <span>one pool for GPU and CPU</span>
        <strong>{nodeVitals(node).memory.text}</strong>
      </header>
      <MemoryIngot label={`${name} unified memory`} memory={memory} size="tall" />
      {memory?.modelsBytes != null && memory.systemBytes != null && (
        <StackedArea
          points={points.map((point) => ({
            t: point.t,
            values: [
              point.modelsBytes === null ? null : point.modelsBytes / GIB,
              point.systemBytes === null ? null : point.systemBytes / GIB,
            ],
          }))}
          layers={LAYERS}
          start={end - HISTORY_SECONDS}
          end={end}
          max={Number(memory.totalBytes) / GIB}
          unit="GiB"
          name={`Models and System memory, last 30 minutes, now ${formatBytes(memory.modelsBytes)} and ${formatBytes(memory.systemBytes)}`}
          empty={
            <p className="spark-panel-note">No history yet. It builds while this page is open.</p>
          }
        />
      )}
      <p className="spark-panel-note">
        Models is what the GPU driver reports for compute processes: weights, KV cache and
        workspace. System is everything else in use. A full-node model holding the pool is normal; a
        warning shows only when free memory drops under {formatBytes(MEMORY_WARN_FREE_BYTES)}.
      </p>
    </section>
  );
}
