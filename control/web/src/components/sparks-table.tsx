import { useMemo, useState } from "react";
import type { VisualFleetNode } from "../api/types";
import {
  nodeDiskFreeBytes,
  nodeDisplayName,
  nodeMemory,
  nodeRecipeUpdates,
  nodeSecondaryName,
  nodeStatus,
  nodeStatusTone,
  nodeVitals,
  nodeWorkloadLines,
  type NodeStatus,
} from "../lib/fleet";
import { EmptyState } from "./empty-state";
import { Meter } from "./meter";
import { SortHeader, useSort } from "./sort-header";
import { StatusPill } from "./status-pill";

// Problems first when sorted ascending.
const STATUS_RANK: Record<NodeStatus, number> = { offline: 0, "needs attention": 1, online: 2 };

/** Every Spark in one sortable table with a search; `selected` marks the open Spark. */
export function SparksTable({
  nodes,
  now,
  selected,
  onSelect,
}: {
  nodes: VisualFleetNode[];
  now: Date;
  selected: string | null;
  onSelect(id: string): void;
}) {
  const [query, setQuery] = useState("");
  const visibleNodes = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return nodes.filter(
      (node) =>
        !needle ||
        `${nodeDisplayName(node)} ${nodeSecondaryName(node) ?? ""}`
          .toLocaleLowerCase()
          .includes(needle),
    );
  }, [nodes, query]);

  const { sorted, sort, toggle } = useSort<VisualFleetNode, "name" | "status" | "memory" | "disk">(
    visibleNodes,
    { key: "name", descending: false },
    {
      name: (node) => nodeDisplayName(node).toLocaleLowerCase(),
      status: (node) => STATUS_RANK[nodeStatus(node, now).status],
      memory: (node) => nodeMemory(node)?.usedPercent ?? null,
      disk: (node) => nodeDiskFreeBytes(node),
    },
  );

  return (
    <>
      <section className="fleet-discovery" aria-label="Find a Spark">
        <label className="fleet-search">
          <span>Find a Spark</span>
          <input
            type="search"
            value={query}
            placeholder="Friendly name or technical name"
            onChange={(event) => setQuery(event.currentTarget.value)}
          />
        </label>
        <span role="status">
          Showing {visibleNodes.length} of {nodes.length}
        </span>
      </section>
      {visibleNodes.length === 0 && (
        <EmptyState title="No Sparks" description="" filtered onClearFilters={() => setQuery("")} />
      )}
      {visibleNodes.length > 0 && (
        <section className="fleet-compact" aria-label="Fleet table">
          <div className="fleet-table-scroll">
            <table>
              <caption className="sr-only">Fleet health, capacity, and workloads</caption>
              <thead>
                <tr>
                  <SortHeader column="name" label="Spark" sort={sort} onSort={toggle} />
                  <SortHeader column="status" label="Status" sort={sort} onSort={toggle} />
                  <SortHeader column="memory" label="Memory used" sort={sort} onSort={toggle} />
                  <SortHeader column="disk" label="Disk free" sort={sort} onSort={toggle} />
                  <th scope="col">GPU</th>
                  <th scope="col">Serving</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((node) => {
                  const { status, reasons } = nodeStatus(node, now);
                  const name = nodeDisplayName(node);
                  const vitals = nodeVitals(node);
                  const workloads = nodeWorkloadLines(node, nodes);
                  return (
                    <tr key={node.id}>
                      <th scope="row">
                        <a
                          href={`?spark=${encodeURIComponent(node.id)}`}
                          aria-current={selected === node.id ? "true" : undefined}
                          onClick={(event) => {
                            event.preventDefault();
                            onSelect(node.id);
                          }}
                        >
                          <strong>{name}</strong>
                        </a>
                        {nodeSecondaryName(node) && <small>{nodeSecondaryName(node)}</small>}
                      </th>
                      <td data-label="Status">
                        <StatusPill tone={nodeStatusTone(status)}>{status}</StatusPill>
                        {reasons.length > 0 && (
                          <small title={reasons.join("\n")}>
                            {reasons[0]}
                            {reasons.length > 1 ? ` +${reasons.length - 1} more` : ""}
                          </small>
                        )}
                      </td>
                      <td data-label="Memory used">
                        <Meter label={`${name} memory used`} vital={vitals.memory} />
                      </td>
                      <td data-label="Disk free">
                        <Meter label={`${name} disk free`} vital={vitals.disk} />
                      </td>
                      <td data-label="GPU">
                        <Meter label={`${name} GPU utilisation`} vital={vitals.gpu} />
                      </td>
                      <td data-label="Serving">
                        {workloads.length === 0 ? (
                          <strong>Idle</strong>
                        ) : (
                          workloads.map((line) => <strong key={line}>{line}</strong>)
                        )}
                        <small>
                          {node.installed.filter((item) => item.complete === true).length}
                          {node.installed.some((item) => item.complete == null) ? " confirmed" : ""}{" "}
                          installed
                        </small>
                        {node.installed.some((item) => item.complete == null) && (
                          <small>Some installation state is unknown</small>
                        )}
                        {nodeRecipeUpdates(node).length > 0 && (
                          <small>
                            <StatusPill tone="info">update available</StatusPill>
                          </small>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}
