import "./chart.css";
import { useId, useState } from "react";
import type { KeyboardEvent, PointerEvent } from "react";
import { linePath, linearScale, niceTicks, runsOf, timeLabel, washPath } from "./scale";

export type ChartSeries = {
  key: string;
  name: string;
  /** A CSS colour expression, always a variable such as `var(--series-1)`. */
  color: string;
  /** Unix seconds and the value in display units; null is a missing sample and leaves a gap. */
  points: readonly { t: number; v: number | null }[];
};

const W = 600;
const H = 200;
const LEFT = 50;
const RIGHT = 20;
const TOP = 12;
const BOTTOM = 30;

/**
 * One-axis time chart: grid, clean ticks, one line per series (a wash under a single series), an end
 * dot, an optional threshold line, a crosshair with tooltip (pointer or arrow keys), and a table view.
 * Every colour is a CSS variable; the values arrive formatted by the caller's `format`.
 */
export function AreaChart({
  title,
  label,
  series,
  start,
  end,
  format,
  threshold,
  live = false,
  emptyText = "No samples in this range",
}: {
  title: string;
  /** The sentence that names the chart to assistive technology. */
  label: string;
  series: readonly ChartSeries[];
  start: number;
  end: number;
  format: (value: number) => string;
  threshold?: { value: number; label: string };
  /** The newest reading is live: the end dot gets a halo (off under reduced motion). */
  live?: boolean;
  emptyText?: string;
}) {
  const gradient = useId();
  const [cursor, setCursor] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const times = [
    ...new Set(series.flatMap((s) => s.points.flatMap((p) => (p.v === null ? [] : [p.t])))),
  ].sort((a, b) => a - b);
  if (!times.length) return <p role="status">{emptyText}</p>;
  const values = series.flatMap((s) => s.points.flatMap((p) => (p.v === null ? [] : [p.v])));
  const ticks = niceTicks(Math.min(0, ...values), Math.max(1, ...values));
  const domain = [ticks[0], ticks[ticks.length - 1]] as const;
  const x = linearScale([start, end], [LEFT, W - RIGHT]);
  const y = linearScale(domain, [H - BOTTOM, TOP]);
  const span = end - start;
  const at = cursor === null ? null : times[cursor];

  function move(event: PointerEvent<SVGSVGElement>) {
    const box = event.currentTarget.getBoundingClientRect();
    if (!box.width) return;
    const t =
      start + ((((event.clientX - box.left) / box.width) * W - LEFT) / (W - LEFT - RIGHT)) * span;
    let nearest = 0;
    times.forEach((time, i) => {
      if (Math.abs(time - t) < Math.abs(times[nearest] - t)) nearest = i;
    });
    setCursor(nearest);
  }
  function key(event: KeyboardEvent<SVGSVGElement>) {
    const last = times.length - 1;
    const next = {
      ArrowLeft: Math.max(0, (cursor ?? last + 1) - 1),
      ArrowRight: Math.min(last, (cursor ?? -1) + 1),
      Home: 0,
      End: last,
    }[event.key];
    if (event.key === "Escape") setCursor(null);
    if (next === undefined) return;
    event.preventDefault();
    setCursor(next);
  }
  const drawn = series.map((s) => {
    const runs = runsOf(s.points, x, y);
    return { s, runs, tail: runs.at(-1)?.at(-1) };
  });
  // Each series' samples by time, for the tooltip and the table.
  const valueAt = new Map(series.map((s) => [s.key, new Map(s.points.map((p) => [p.t, p.v]))]));
  const readout = (s: ChartSeries, t: number) => {
    const value = valueAt.get(s.key)?.get(t);
    return value === null || value === undefined ? "no sample" : format(value);
  };
  const tooltipAt = at === null ? null : x(at) / W;
  return (
    <div className="chart">
      <div className="chart-plot">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          role="img"
          aria-label={label}
          tabIndex={0}
          onPointerMove={move}
          onPointerLeave={() => setCursor(null)}
          onKeyDown={key}
          onBlur={() => setCursor(null)}
        >
          {ticks.map((tick) => (
            <g key={tick}>
              <line className="chart-grid" x1={LEFT} x2={W - RIGHT} y1={y(tick)} y2={y(tick)} />
              <text className="chart-tick chart-tick-y" x={LEFT - 8} y={y(tick) + 4}>
                {format(tick)}
              </text>
            </g>
          ))}
          {[start, start + span / 2, end].map((t, i) => (
            <text key={t} className={`chart-tick chart-tick-x${i}`} x={x(t)} y={H - 8}>
              {timeLabel(t, span)}
            </text>
          ))}
          {threshold && (
            <g>
              <line
                className="chart-threshold"
                x1={LEFT}
                x2={W - RIGHT}
                y1={y(threshold.value)}
                y2={y(threshold.value)}
              />
              <text className="chart-tick chart-tick-y" x={W - RIGHT} y={y(threshold.value) - 5}>
                {threshold.label}
              </text>
            </g>
          )}
          {series.length === 1 && (
            <>
              <defs>
                <linearGradient id={gradient} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0" style={{ stopColor: series[0].color, stopOpacity: 0.18 }} />
                  <stop offset="1" style={{ stopColor: series[0].color, stopOpacity: 0 }} />
                </linearGradient>
              </defs>
              <path d={washPath(drawn[0].runs, y(domain[0]))} fill={`url(#${gradient})`} />
            </>
          )}
          {drawn.map(({ s, runs, tail }) => (
            <g key={s.key}>
              <path className="chart-line" d={linePath(runs)} style={{ stroke: s.color }} />
              {tail && live && (
                <circle
                  className="chart-halo"
                  cx={tail[0]}
                  cy={tail[1]}
                  r="10"
                  style={{ fill: s.color }}
                />
              )}
              {tail && (
                <circle
                  className="chart-dot"
                  cx={tail[0]}
                  cy={tail[1]}
                  r="4"
                  style={{ fill: s.color }}
                />
              )}
            </g>
          ))}
          {at !== null && (
            <g className="chart-crosshair">
              <line x1={x(at)} x2={x(at)} y1={TOP} y2={H - BOTTOM} />
              {series.flatMap((s) => {
                const value = valueAt.get(s.key)?.get(at);
                return value !== null && value !== undefined
                  ? [
                      <circle
                        key={s.key}
                        className="chart-dot"
                        cx={x(at)}
                        cy={y(value)}
                        r="4"
                        style={{ fill: s.color }}
                      />,
                    ]
                  : [];
              })}
            </g>
          )}
        </svg>
        {at !== null && tooltipAt !== null && (
          <div
            className="chart-tooltip"
            role="tooltip"
            style={{ left: `${tooltipAt * 100}%` }}
            data-flipped={tooltipAt > 0.6}
          >
            <strong>{timeLabel(at, span)}</strong>
            {series.map((s) => (
              <span key={s.key}>
                {s.name}: {readout(s, at)}
              </span>
            ))}
          </div>
        )}
      </div>
      <button
        type="button"
        className="button quiet chart-table-toggle"
        aria-pressed={table}
        onClick={() => setTable((value) => !value)}
      >
        {table ? "Show as chart" : "Show as table"}
      </button>
      {table && (
        <table className="chart-table">
          <caption>{title}</caption>
          <thead>
            <tr>
              <th scope="col">Time</th>
              {series.map((s) => (
                <th key={s.key} scope="col">
                  {s.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {times.map((t) => (
              <tr key={t}>
                <th scope="row">{timeLabel(t, span)}</th>
                {series.map((s) => (
                  <td key={s.key}>{readout(s, t)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
