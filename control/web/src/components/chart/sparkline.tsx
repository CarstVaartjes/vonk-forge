import "./chart.css";
import { useId } from "react";
import { linePath, linearScale, type Point, runsOf, washPath } from "./scale";

const W = 120;
const H = 36;
const PAD = 5;

/**
 * A trace with a soft wash and an end dot, no axes: the number beside it is the label. Points sit at
 * their time between start and end; a missing sample leaves a gap. With nothing to draw it renders
 * nothing, so the caller says "Not reported".
 */
export function Sparkline({
  points,
  start,
  end,
  min,
  max,
  color = "var(--model-ink)",
  label,
}: {
  points: readonly Point[];
  start: number;
  end: number;
  min?: number;
  max?: number;
  color?: string;
  label: string;
}) {
  const id = useId();
  const present = points.flatMap((point) => (point.v === null ? [] : [point.v]));
  if (!present.length) return null;
  const lo = min ?? Math.min(...present);
  const hi = max ?? Math.max(...present);
  const x = linearScale([start, end], [0, W - 6]);
  const y = linearScale([lo, hi], [H - PAD, PAD]);
  const runs = runsOf(
    points.map((point) => ({
      t: point.t,
      v: point.v === null ? null : Math.min(hi, Math.max(lo, point.v)),
    })),
    x,
    y,
  );
  const tail = runs.at(-1)?.at(-1) ?? [0, 0];
  const wash = washPath(runs, H);
  const line = linePath(runs);
  return (
    <svg className="sparkline" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label}>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style={{ stopColor: color, stopOpacity: 0.2 }} />
          <stop offset="1" style={{ stopColor: color, stopOpacity: 0 }} />
        </linearGradient>
      </defs>
      {wash && <path d={wash} fill={`url(#${id})`} />}
      {line && <path d={line} className="chart-line" style={{ stroke: color }} />}
      <circle cx={tail[0]} cy={tail[1]} r="3.5" className="chart-dot" style={{ fill: color }} />
    </svg>
  );
}
