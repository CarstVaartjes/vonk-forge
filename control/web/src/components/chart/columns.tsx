import "./chart.css";
const H = 80;
const MAX_WIDTH = 24;

/** Columns: at most 24 px wide, a 4 px rounded top, square at the baseline; the last one is the current period. */
export function Columns({
  values,
  label,
  max,
}: {
  values: readonly (number | null)[];
  label: string;
  max?: number;
}) {
  const present = values.flatMap((value) => (value === null ? [] : [value]));
  if (!present.length) return null;
  const hi = max ?? Math.max(...present, 1) * 1.08;
  const slot = 600 / values.length;
  const width = Math.min(MAX_WIDTH, Math.max(3, slot - 3));
  return (
    <svg className="columns" viewBox={`0 0 600 ${H}`} role="img" aria-label={label}>
      {values.map((value, i) => {
        if (value === null) return null;
        const height = Math.max(2, (value / hi) * (H - 4));
        const left = i * slot + (slot - width) / 2;
        const top = H - height;
        const r = Math.min(4, width / 2, height);
        return (
          <path
            key={i}
            className={i === values.length - 1 ? "column column-now" : "column"}
            d={`M${left} ${H}V${top + r}Q${left} ${top} ${left + r} ${top}H${left + width - r}Q${left + width} ${top} ${left + width} ${top + r}V${H}Z`}
          />
        );
      })}
    </svg>
  );
}
