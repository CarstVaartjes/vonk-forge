import { NOT_REPORTED_TEXT } from "../lib/fleet";
import { Sparkline } from "./chart/sparkline";

/**
 * One reading and the last 30 minutes behind it: its title, the figure (null says Not reported and draws no
 * trace), and the trace in the Spark's colour. A fixed range pins the trace to a 0 to 100 scale.
 */
export function TraceTile({
  title,
  shown,
  unit,
  points,
  start,
  end,
  color,
  label,
  fixed = false,
}: {
  title: string;
  shown: string | null;
  unit: string;
  points: readonly { t: number; v: number | null }[];
  start: number;
  end: number;
  color: string;
  label: string;
  fixed?: boolean;
}) {
  return (
    <div className="spark-trace">
      <div className="spark-trace-head">
        <span>{title}</span>
        <span className="spark-trace-value">
          {shown === null ? (
            NOT_REPORTED_TEXT
          ) : (
            <>
              {shown} <small>{unit}</small>
            </>
          )}
        </span>
      </div>
      <Sparkline
        points={points}
        start={start}
        end={end}
        min={fixed ? 0 : undefined}
        max={fixed ? 100 : undefined}
        color={color}
        label={label}
      />
    </div>
  );
}
