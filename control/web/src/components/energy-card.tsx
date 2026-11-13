import "./stat-card.css";
import type { MetricsSeriesResponse } from "../api/types";
import { NOT_REPORTED_TEXT } from "../lib/fleet";
import { fleetFigure, formatValue } from "../lib/metric-units";
import { Estimate, EstimateTag } from "./estimate";

/** What every GPU energy and power figure says about its scope. */
export const GPU_ONLY_CAPTION = "GPU only, not the whole Spark.";

/**
 * GPU energy over 24 hours: the Controller's fleet total of the gpu_energy metric (an estimate, so "≈" and
 * "est."), and GPU power now from its fleet line. The 31-day and per-token facts the Controller does not
 * serve say Not reported. It is always "GPU energy" (never "energy" or "power" alone) and carries no
 * price, cost or whole-Spark figure.
 */
export function EnergyCard({
  energy,
  power,
}: {
  energy: MetricsSeriesResponse | null;
  power: MetricsSeriesResponse | null;
}) {
  const total = fleetFigure(energy, "total");
  const watts = fleetFigure(power, "latest");
  return (
    <section className="card stat-card" aria-labelledby="energy-card-heading">
      <header>
        <h2 id="energy-card-heading">GPU energy</h2>
        <small>last 24 hours</small>
      </header>
      <p className="stat-fig">
        {total ? (
          <>
            <Estimate basis={total.basis}>{formatValue(total.value, total.symbol)}</Estimate>
            <EstimateTag basis={total.basis} />
          </>
        ) : (
          NOT_REPORTED_TEXT
        )}
      </p>
      <p className="stat-caption">{total ? total.hint : GPU_ONLY_CAPTION}</p>
      <dl className="stat-facts">
        <div>
          <dt>GPU power now</dt>
          <dd>{watts ? `${watts.value.toFixed(1)} ${watts.symbol}` : NOT_REPORTED_TEXT}</dd>
        </div>
        <div>
          <dt>Last 31 days</dt>
          <dd>{NOT_REPORTED_TEXT}</dd>
        </div>
        <div>
          <dt>Per 1,000 tokens</dt>
          <dd>{NOT_REPORTED_TEXT}</dd>
        </div>
      </dl>
    </section>
  );
}
