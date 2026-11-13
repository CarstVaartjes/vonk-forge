import "./chart.css";
import { NOT_REPORTED_TEXT } from "../../lib/fleet";

const SIZE = 120;
const CENTER = SIZE / 2;
const RADIUS = 46;
/** The arc opens at the bottom: 240 degrees of sweep from lower left, clockwise, to lower right. */
const START_DEG = 150;
const SWEEP_DEG = 240;

function pointAt(deg: number, radius: number): [number, number] {
  const rad = (deg * Math.PI) / 180;
  return [CENTER + radius * Math.cos(rad), CENTER + radius * Math.sin(rad)];
}

function arc(from: number, to: number): string {
  const [x0, y0] = pointAt(from, RADIUS);
  const [x1, y1] = pointAt(to, RADIUS);
  return `M${x0.toFixed(2)} ${y0.toFixed(2)}A${RADIUS} ${RADIUS} 0 ${to - from > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

/**
 * One reading against its scale as an arc, the number in the middle. The ticks mark lines (a warning, a
 * limit); the caption under the arc says in words what each one is, so no line is only a colour. An absent reading draws no arc and says "Not reported"; a reading without a known scale
 * shows its number and no arc.
 */
export function ArcGauge({
  title,
  value,
  unit,
  max,
  ticks = [],
  caption,
  color,
}: {
  title: string;
  value: number | null;
  unit: string;
  max: number | null;
  ticks?: readonly { value: number; tone: "warn" | "bad" | "limit" }[];
  caption?: string;
  color: string;
}) {
  const shown = value === null ? NOT_REPORTED_TEXT : `${value.toLocaleString("en-US")} ${unit}`;
  const scaled = value !== null && max !== null && max > 0;
  const toDeg = (v: number) => START_DEG + SWEEP_DEG * Math.min(1, Math.max(0, v / (max ?? 1)));
  // Only a reading with a scale is a meter; anything else is plain text, so no meter lacks its maximum.
  const meter = scaled
    ? {
        role: "meter",
        "aria-label": title,
        "aria-valuemin": 0,
        "aria-valuemax": max,
        "aria-valuenow": value,
        "aria-valuetext": [shown, caption].filter(Boolean).join(", "),
      }
    : {};
  return (
    <div className="arc-gauge" {...meter}>
      <div className="arc-gauge-dial">
        {scaled && (
          <svg viewBox={`0 0 ${SIZE} ${SIZE - 14}`} aria-hidden="true" focusable="false">
            <path className="arc-track" d={arc(START_DEG, START_DEG + SWEEP_DEG)} />
            {value > 0 && (
              <path
                className="arc-value"
                d={arc(START_DEG, toDeg(value))}
                style={{ stroke: color }}
              />
            )}
            {ticks.map((tick) => {
              const [x0, y0] = pointAt(toDeg(tick.value), RADIUS - 8);
              const [x1, y1] = pointAt(toDeg(tick.value), RADIUS + 8);
              return (
                <line
                  key={tick.value}
                  className={`arc-tick arc-tick-${tick.tone}`}
                  x1={x0}
                  y1={y0}
                  x2={x1}
                  y2={y1}
                />
              );
            })}
          </svg>
        )}
        <div className="arc-gauge-text">
          <strong>{shown}</strong>
          <span>{title}</span>
        </div>
      </div>
      {caption && <p className="arc-gauge-caption">{caption}</p>}
    </div>
  );
}
