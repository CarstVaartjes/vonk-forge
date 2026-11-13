import { useId, type MouseEvent } from "react";
import type { VisualFleetNode } from "../api/types";
import { NOT_REPORTED_TEXT, nodeRuns } from "../lib/fleet";
import { sparkPath } from "../lib/spark-route";
import { StatusPill } from "./status-pill";

/**
 * What this Spark serves: each run's alias, title and state, its rank and the Sparks it shares the run
 * with (as links), and the decode speed, which no source reports yet. A Spark with no run says so, with
 * how many recipes it has installed.
 */
export function SparkServing({
  node,
  nodes,
  onNavigate,
}: {
  node: VisualFleetNode;
  nodes: readonly VisualFleetNode[];
  onNavigate(event: MouseEvent<HTMLAnchorElement>, path: string): void;
}) {
  const id = useId();
  const runs = nodeRuns(node, nodes);
  const installed = node.installed.filter((item) => item.complete === true).length;
  return (
    <section className="card spark-panel-card" aria-labelledby={id}>
      <header>
        <h2 id={id}>Serving</h2>
      </header>
      {runs.length === 0 && (
        <p className="spark-panel-note">
          Nothing is loaded. {installed} {installed === 1 ? "recipe is" : "recipes are"} installed.
        </p>
      )}
      {runs.map((run) => (
        <div className="spark-run" key={run.runId}>
          <p>
            {run.alias && <code className="unit-alias">{run.alias}</code>}{" "}
            <strong>{run.title}</strong> <StatusPill tone={run.tone}>{run.state}</StatusPill>
          </p>
          {run.rank !== null && run.rankCount !== null && run.rankCount > 1 && (
            <p className="spark-panel-note">
              Rank {run.rank} of {run.rankCount}, one unit with{" "}
              {run.partners.map((partner, index) => (
                <span key={partner.id}>
                  {index > 0 && ", "}
                  <a
                    href={sparkPath(partner.id)}
                    onClick={(event) => onNavigate(event, sparkPath(partner.id))}
                  >
                    {partner.name}
                  </a>
                </span>
              ))}
              .
            </p>
          )}
          <p className="spark-panel-note">Decode speed: {NOT_REPORTED_TEXT}</p>
        </div>
      ))}
    </section>
  );
}
