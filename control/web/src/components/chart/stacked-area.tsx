import "./chart.css";
import type { ReactNode } from "react";
import { linearScale, niceTicks, timeLabel } from "./scale";

type Point = Parameters<typeof StackedArea>[0]["points"][number];
const W = 600;
const H = 150;
const LEFT = 34;
const TOP = 6;
const BOTTOM = 22;

/**
 * Layers stacked from the bottom (first layer first) over time, with a 2 px surface gap between them,
 * a grid and a time axis. A point with any layer missing is a gap: no area crosses it. With nothing to
 * draw it renders nothing, so the caller says "Not reported". `name` is the sentence a screen reader
 * hears.
 */
export function StackedArea({
  points,
  layers,
  start,
  end,
  max,
  unit,
  name,
  empty = null,
}: {
  points: readonly { t: number; values: readonly (number | null)[] }[];
  layers: readonly { key: string; color: string }[];
  start: number;
  end: number;
  max: number;
  unit: string;
  name: string;
  /** Shown instead of the chart when no two complete points are in a row. */
  empty?: ReactNode;
}) {
  const complete = points.map((point) =>
    point.values.length === layers.length && point.values.every((v) => v !== null) ? point : null,
  );
  // Each run of complete points is one stack; layer i sits between the sum below it and the sum through it.
  const runs: Point[][] = [[]];
  for (const point of complete) {
    const current = runs[runs.length - 1] as Point[];
    if (point) current.push(point);
    else if (current.length) runs.push([]);
  }
  if (!runs.some((run) => run.length > 1)) return empty;
  const x = linearScale([start, end], [LEFT, W]);
  const ticks = niceTicks(0, max, 3);
  const top = ticks[ticks.length - 1] ?? max;
  const y = linearScale([0, top], [H - BOTTOM, TOP]);
  const sums = (point: Point, upTo: number) =>
    (point.values as number[]).slice(0, upTo).reduce((a, b) => a + b, 0);
  const shapes = layers.map((layer, i) =>
    runs
      .filter((run) => run.length > 1)
      .map((run) => {
        const upper = run.map((p) => `${x(p.t).toFixed(1)} ${y(sums(p, i + 1)).toFixed(1)}`);
        const lower = [...run]
          .reverse()
          .map((p) => `${x(p.t).toFixed(1)} ${y(sums(p, i)).toFixed(1)}`);
        return `M${upper.join("L")}L${lower.join("L")}Z`;
      })
      .join(""),
  );
  return (
    <svg className="stacked-area" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={name}>
      {ticks.map((tick) => (
        <g key={tick}>
          <line className="chart-grid" x1={LEFT} x2={W} y1={y(tick)} y2={y(tick)} />
          <text className="chart-tick chart-tick-y" x={LEFT - 6} y={y(tick) + 4}>
            {tick}
          </text>
        </g>
      ))}
      {layers.map((layer, i) => (
        <path key={layer.key} className="stack-layer" d={shapes[i]} style={{ fill: layer.color }} />
      ))}
      <text className="chart-tick chart-tick-x0" x={LEFT} y={H - 4}>
        {timeLabel(start, end - start)}
      </text>
      <text className="chart-tick chart-tick-x2" x={W} y={H - 4}>
        now
      </text>
      <title>{`${name} (${unit})`}</title>
    </svg>
  );
}
