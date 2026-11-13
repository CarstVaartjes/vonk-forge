import type { MetricRange } from "../api/types";

export const RANGES: readonly MetricRange[] = ["1h", "6h", "24h", "7d", "30d"];

/** One range for every chart on a page; the page owns the state. */
export function RangeSwitch({
  range,
  onChange,
}: {
  range: MetricRange;
  onChange(range: MetricRange): void;
}) {
  return (
    <div className="segmented" role="group" aria-label="Range">
      {RANGES.map((value) => (
        <button
          key={value}
          type="button"
          aria-pressed={range === value}
          onClick={() => onChange(value)}
        >
          {value}
        </button>
      ))}
    </div>
  );
}
