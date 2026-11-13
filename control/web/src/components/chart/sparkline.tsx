import "./chart.css";
import { useId } from "react";
import { linearScale } from "./scale";

const W = 120;
const H = 36;
const PAD = 5;

/**
 * A trace with a soft wash and an end dot, no axes: the number beside it is the label. Missing
 * samples leave a gap. With nothing to draw it renders nothing, so the caller says "Not reported".
 */
export function Sparkline({
  values,
  min,
  max,
  color = "var(--model-ink)",
  label,
}: {
  values: readonly (number | null)[];
  min?: number;
  max?: number;
  color?: string;
  label: string;
}) {
  const id = useId();
  const present = values.flatMap((value) => (value === null ? [] : [value]));
  if (!present.length) return null;
  const lo = min ?? Math.min(...present);
  const hi = max ?? Math.max(...present);
  const x = linearScale([0, Math.max(1, values.length - 1)], [0, W - 6]);
  const y = linearScale([lo, hi], [H - PAD, PAD]);
  const clamp = (value: number) => Math.min(hi, Math.max(lo, value));
  let line = "";
  let washes = "";
  let run: [number, number][] = [];
  let last: [number, number] = [0, 0];
  const flush = () => {
    if (run.length > 1) {
      const d = run
        .map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)} ${py.toFixed(1)}`)
        .join("");
      line += d;
      washes += `${d}L${run[run.length - 1][0].toFixed(1)} ${H}L${run[0][0].toFixed(1)} ${H}Z`;
    }
    run = [];
  };
  values.forEach((value, i) => {
    if (value === null) return flush();
    last = [x(i), y(clamp(value))];
    run.push(last);
  });
  flush();
  const end = last;
  return (
    <svg className="sparkline" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label}>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" style={{ stopColor: color, stopOpacity: 0.2 }} />
          <stop offset="1" style={{ stopColor: color, stopOpacity: 0 }} />
        </linearGradient>
      </defs>
      {washes && <path d={washes} fill={`url(#${id})`} />}
      {line && <path d={line} className="chart-line" style={{ stroke: color }} />}
      <circle cx={end[0]} cy={end[1]} r="3.5" className="chart-dot" style={{ fill: color }} />
    </svg>
  );
}
