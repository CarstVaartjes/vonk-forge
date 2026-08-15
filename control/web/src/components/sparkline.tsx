import {useId} from "react";

const WIDTH = 100;
const HEIGHT = 30;
const PADDING = 2;

function coordinate(value: number): string {
  return String(Number(value.toFixed(2)));
}

export function sparklinePath(values: readonly (number | null | undefined)[], width = WIDTH, height = HEIGHT): string {
  const finite = values.filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  if (finite.length === 0 || values.length === 0) return "";
  const minimum = Math.min(...finite);
  const maximum = Math.max(...finite);
  const xSpan = Math.max(0, width - PADDING * 2);
  const ySpan = Math.max(0, height - PADDING * 2);
  const commands: string[] = [];
  let connected = false;
  values.forEach((value, index) => {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      connected = false;
      return;
    }
    const x = values.length === 1
      ? width / 2
      : PADDING + (index / (values.length - 1)) * xSpan;
    const y = maximum === minimum
      ? height / 2
      : PADDING + ((maximum - value) / (maximum - minimum)) * ySpan;
    commands.push(`${connected ? "L" : "M"} ${coordinate(x)} ${coordinate(y)}`);
    connected = true;
  });
  return commands.join(" ");
}

export function Sparkline({
  formatValue = value => Number(value.toFixed(1)).toString(),
  label,
  values,
}: {
  formatValue?: (value: number) => string;
  label: string;
  values: readonly (number | null | undefined)[];
}) {
  const id = useId();
  const titleId = `${id}-title`;
  const descriptionId = `${id}-description`;
  const finite = values.filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const path = sparklinePath(values);
  const latest = finite.at(-1);
  const minimum = finite.length > 0 ? Math.min(...finite) : undefined;
  const maximum = finite.length > 0 ? Math.max(...finite) : undefined;
  const accessibleSummary = latest === undefined || minimum === undefined || maximum === undefined
    ? "No reported samples."
    : `Latest ${formatValue(latest)}; range ${formatValue(minimum)} to ${formatValue(maximum)}; ${finite.length} reported samples.`;
  const visibleSummary = latest === undefined || minimum === undefined || maximum === undefined
    ? "No reported samples"
    : `Latest ${formatValue(latest)} · Range ${formatValue(minimum)}–${formatValue(maximum)} · ${finite.length} samples`;

  return <figure className="sparkline">
    <svg role="img" aria-labelledby={titleId} aria-describedby={descriptionId} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none">
      <title id={titleId}>{label}</title>
      <desc id={descriptionId}>{accessibleSummary}</desc>
      {path && <path aria-hidden="true" d={path} vectorEffect="non-scaling-stroke"/>}
    </svg>
    <figcaption>{visibleSummary}</figcaption>
  </figure>;
}
