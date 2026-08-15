import {useId} from "react";

const WIDTH = 100;
const HEIGHT = 30;
const PADDING = 2;

export type SparklineSeriesPoint = {
  count: number;
  minimum: number;
  mean: number;
  maximum: number;
};

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

function scaledPath(
  values: readonly (number | null | undefined)[],
  minimum: number,
  maximum: number,
  width = WIDTH,
  height = HEIGHT,
): string {
  if (values.length === 0) return "";
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
  sampleLabel,
  values,
  series,
}: {
  formatValue?: (value: number) => string;
  label: string;
  sampleLabel?: "samples" | "buckets";
  values: readonly (number | null | undefined)[];
  series?: readonly (SparklineSeriesPoint | null | undefined)[];
}) {
  const id = useId();
  const titleId = `${id}-title`;
  const descriptionId = `${id}-description`;
  const finite = values.filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const hasSeries = series !== undefined;
  const normalizedSeries = (series ?? []).map(value =>
      value !== null && value !== undefined
      && Number.isFinite(value.count)
      && value.count > 0
      && Number.isFinite(value.minimum)
      && Number.isFinite(value.mean)
      && Number.isFinite(value.maximum)
      ? value
      : null
  );
  const seriesValues = normalizedSeries.filter((value): value is SparklineSeriesPoint => value !== null);
  const seriesMinimum = seriesValues.length > 0
    ? Math.min(...seriesValues.map(value => value.minimum))
    : undefined;
  const seriesMaximum = seriesValues.length > 0
    ? Math.max(...seriesValues.map(value => value.maximum))
    : undefined;
  const seriesLatest = seriesValues.at(-1)?.mean;
  const seriesMean = seriesValues.length > 0
    ? seriesValues.reduce((total, value) => total + value.mean * value.count, 0)
      / seriesValues.reduce((total, value) => total + value.count, 0)
    : undefined;
  const minimum = hasSeries ? seriesMinimum : finite.length > 0 ? Math.min(...finite) : undefined;
  const maximum = hasSeries ? seriesMaximum : finite.length > 0 ? Math.max(...finite) : undefined;
  const latest = hasSeries ? seriesLatest : finite.at(-1);
  const reportedLabel = sampleLabel ?? (hasSeries ? "buckets" : "samples");
  const accessibleSummary = hasSeries
    ? seriesMean === undefined || minimum === undefined || maximum === undefined
      ? "No reported samples."
      : `Mean ${formatValue(seriesMean)}; latest mean ${formatValue(latest ?? seriesMean)}; reported range ${formatValue(minimum)} to ${formatValue(maximum)}; ${seriesValues.length} reported ${reportedLabel}.`
    : latest === undefined || minimum === undefined || maximum === undefined
      ? "No reported samples."
      : `Latest ${formatValue(latest)}; range ${formatValue(minimum)} to ${formatValue(maximum)}; ${finite.length} reported samples.`;
  const visibleSummary = hasSeries
    ? seriesMean === undefined || minimum === undefined || maximum === undefined
      ? "No reported samples"
      : `Mean ${formatValue(seriesMean)} · Range ${formatValue(minimum)}–${formatValue(maximum)} · ${seriesValues.length} reported ${reportedLabel}`
    : latest === undefined || minimum === undefined || maximum === undefined
      ? "No reported samples"
      : `Latest ${formatValue(latest)} · Range ${formatValue(minimum)}–${formatValue(maximum)} · ${finite.length} samples`;
  const path = hasSeries && minimum !== undefined && maximum !== undefined
    ? scaledPath(normalizedSeries.map(value => value?.mean), minimum, maximum)
    : sparklinePath(values);
  const minimumPath = hasSeries && minimum !== undefined && maximum !== undefined
    ? scaledPath(normalizedSeries.map(value => value?.minimum), minimum, maximum)
    : "";
  const maximumPath = hasSeries && minimum !== undefined && maximum !== undefined
    ? scaledPath(normalizedSeries.map(value => value?.maximum), minimum, maximum)
    : "";

  return <figure className="sparkline">
    <svg role="img" aria-labelledby={titleId} aria-describedby={descriptionId} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none">
      <title id={titleId}>{label}</title>
      <desc id={descriptionId}>{accessibleSummary}</desc>
      {maximumPath && <path className="sparkline-range-maximum" aria-hidden="true" d={maximumPath} vectorEffect="non-scaling-stroke"/>}
      {minimumPath && <path className="sparkline-range-minimum" aria-hidden="true" d={minimumPath} vectorEffect="non-scaling-stroke"/>}
      {path && <path aria-hidden="true" d={path} vectorEffect="non-scaling-stroke"/>}
    </svg>
    <figcaption>{visibleSummary}</figcaption>
  </figure>;
}
