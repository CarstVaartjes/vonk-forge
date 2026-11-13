import "./stat-card.css";
import type { VisualFleetNode } from "../api/types";
import { fleetPower } from "../lib/fleet";

const NOT_REPORTED = "Not reported";
/** What every GPU energy and power figure says about its scope. */
export const GPU_ONLY_CAPTION = "GPU only, not the whole Spark.";

/**
 * GPU energy over 24 hours. The Controller has no energy metric yet, so the figure and the 31-day and
 * per-token facts say Not reported; the card is the label and the place they will fill. It is always
 * "GPU energy" (never "energy" or "power" alone) and carries no price, cost or whole-Spark figure.
 */
export function EnergyCard({ nodes }: { nodes: VisualFleetNode[] }) {
  const power = fleetPower(nodes);
  return (
    <section className="card stat-card" aria-labelledby="energy-card-heading">
      <header>
        <h2 id="energy-card-heading">GPU energy</h2>
        <small>last 24 hours</small>
      </header>
      <p className="stat-fig">{NOT_REPORTED}</p>
      <p className="stat-caption">{GPU_ONLY_CAPTION}</p>
      <dl className="stat-facts">
        <div>
          <dt>GPU power now</dt>
          <dd>{power ? `${power.watts.toFixed(1)} W` : NOT_REPORTED}</dd>
        </div>
        <div>
          <dt>Last 31 days</dt>
          <dd>{NOT_REPORTED}</dd>
        </div>
        <div>
          <dt>Per 1,000 tokens</dt>
          <dd>{NOT_REPORTED}</dd>
        </div>
      </dl>
    </section>
  );
}
