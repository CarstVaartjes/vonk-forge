import { useId } from "react";
import type { VisualFleetNode } from "../api/types";
import { HISTORY_SECONDS, type HistoryPoint } from "../hooks/use-telemetry-history";
import { NOT_REPORTED_TEXT, halfUp, liveSample, nodeDisplayName } from "../lib/fleet";
import { SENSOR_READINGS } from "../lib/sensor-readings";
import { TraceTile } from "./trace-tile";

/**
 * The board sensors as trace tiles: each reading the Spark's live sample carries, with the last 30
 * minutes behind it. A sensor the Spark does not report is left out; with none reported the card says so.
 */
export function SensorStrip({
  node,
  points,
  now,
  color,
}: {
  node: VisualFleetNode;
  points: readonly HistoryPoint[];
  now: Date;
  color: string;
}) {
  const id = useId();
  const sample = liveSample(node);
  const end = now.getTime() / 1000;
  const tiles = SENSOR_READINGS.flatMap((reading) => {
    const value = sample ? reading.now(sample) : null;
    return value === null ? [] : [{ reading, value }];
  });
  return (
    <section className="card spark-panel-card spark-strip" aria-labelledby={id}>
      <header>
        <h2 id={id}>Sensors</h2>
        <span>last 30 minutes</span>
      </header>
      {tiles.length === 0 && <p className="spark-panel-note">{NOT_REPORTED_TEXT}</p>}
      <div className="spark-sensors">
        {tiles.map(({ reading, value }) => (
          <TraceTile
            key={reading.title}
            title={reading.title}
            shown={String(halfUp(value))}
            unit={reading.unit}
            points={points.map((point) => ({ t: point.t, v: reading.past(point) }))}
            start={end - HISTORY_SECONDS}
            end={end}
            color={color}
            label={`${nodeDisplayName(node)} ${reading.title}, last 30 minutes`}
          />
        ))}
      </div>
    </section>
  );
}
