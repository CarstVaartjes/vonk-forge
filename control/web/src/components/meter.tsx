import type {StatusTone} from "./status-pill";

type MeterProps = {
  label: string;
  max?: number;
  tone?: StatusTone;
  value: number;
  valueLabel?: string;
};

export function Meter({label, max = 100, tone = "healthy", value, valueLabel}: MeterProps) {
  const safeMax = Number.isFinite(max) && max > 0 ? max : 100;
  const safeValue = Math.min(safeMax, Math.max(0, Number.isFinite(value) ? value : 0));
  const displayedValue = valueLabel ?? `${Math.round((safeValue / safeMax) * 100)}%`;
  return <div className={`meter meter-${tone}`}>
    <div className="meter-heading"><span>{label}</span><strong>{displayedValue}</strong></div>
    <meter aria-label={label} min={0} max={safeMax} value={safeValue}>{displayedValue}</meter>
  </div>;
}
