import { useId } from "react";
import type { VisualFleetNode } from "../api/types";
import {
  NOT_REPORTED_TEXT,
  formatBytes,
  nodeDiskSplit,
  nodeDisplayName,
  nodeVitals,
} from "../lib/fleet";
import { MemoryIngot } from "./memory-ingot";

/**
 * The Spark's disk as Used and Free, and the models on it: each installation's title and the size the
 * Spark reports for it, "in use" when a loaded run is that installation. A size the Spark does not
 * report says so; the Used part is the disk's own figure, not a sum of model sizes.
 */
export function SparkStorage({ node }: { node: VisualFleetNode }) {
  const id = useId();
  const inUse = new Set(node.loaded.map((run) => run.installation_id));
  return (
    <section className="card spark-panel-card" aria-labelledby={id}>
      <header>
        <h2 id={id}>Storage</h2>
        <strong>{nodeVitals(node).disk.text}</strong>
      </header>
      <MemoryIngot
        label={`${nodeDisplayName(node)} disk`}
        memory={nodeDiskSplit(node)}
        warn={false}
      />
      <ul className="spark-models" aria-label="Models on disk">
        {node.installed.map((item) => (
          <li key={item.installation_id}>
            <span>{item.title ?? item.installation_id}</span>
            <span>
              {item.installed_bytes == null ? NOT_REPORTED_TEXT : formatBytes(item.installed_bytes)}
              {inUse.has(item.installation_id) ? " · in use" : ""}
            </span>
          </li>
        ))}
      </ul>
      <p className="spark-panel-note">Unused model files are removed when space runs short.</p>
    </section>
  );
}
