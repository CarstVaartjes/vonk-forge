import type { MetricSpec, MetricsSeriesResponse } from "../api/types";

const GIB = 1024 ** 3;
// How the Controller's unit names read on the page; titles and basis come from the catalogue.
export const UNITS: Record<MetricSpec["unit"], { symbol: string; scale: number }> = {
  percent: { symbol: "%", scale: 1 },
  bytes: { symbol: "GiB", scale: GIB },
  celsius: { symbol: "°C", scale: 1 },
  watts: { symbol: "W", scale: 1 },
  watt_hours: { symbol: "Wh", scale: 1 },
  megahertz: { symbol: "MHz", scale: 1 },
  rpm: { symbol: "rpm", scale: 1 },
  per_second: { symbol: "/s", scale: 1 },
  bytes_per_second: { symbol: "GiB/s", scale: GIB },
  tokens_per_second: { symbol: "tok/s", scale: 1 },
  seconds: { symbol: "s", scale: 1 },
  ratio: { symbol: "%", scale: 0.01 },
  count: { symbol: "", scale: 1 },
};
const BINARY = [
  ["TiB", 1024 ** 4],
  ["GiB", GIB],
  ["MiB", 1024 ** 2],
  ["KiB", 1024],
] as const;

/** Two decimals below 10, one below 100, whole above, without trailing zeros. */
function short(value: number): string {
  const digits = Math.abs(value) < 10 ? 2 : Math.abs(value) < 100 ? 1 : 0;
  return String(Number(value.toFixed(digits)));
}

function size(bytes: number): string {
  const step = BINARY.find(([, divisor]) => bytes >= divisor);
  return step ? `${(bytes / step[1]).toFixed(1)} ${step[0]}` : `${bytes.toFixed(0)} B`;
}

/**
 * A number in its unit symbol (the value already divided by the unit's scale), by the wording rules
 * in tests/fixtures/metrics-wording.json that `vonkctl metrics` follows too.
 */
export function formatValue(value: number, unit: string): string {
  switch (unit) {
    case "tok/s":
      return `${Math.abs(value) < 100 ? value.toFixed(1) : value.toFixed(0)} tok/s`;
    case "s":
      return Math.abs(value) < 1 ? `${(value * 1000).toFixed(0)} ms` : `${value.toFixed(2)} s`;
    case "W":
    case "°C":
    case "rpm":
    case "MHz":
      return `${value.toFixed(0)} ${unit}`;
    case "Wh":
      return Math.abs(value) >= 1000
        ? `${(value / 1000).toFixed(2)} kWh`
        : `${value.toFixed(0)} Wh`;
    case "GiB":
      return size(value * GIB);
    case "GiB/s":
      return `${size(value * GIB)}/s`;
    case "%":
      return `${value.toFixed(1)}%`;
    case "/s":
      return `${short(value)}/s`;
    case "":
      return short(value);
    default:
      return `${short(value)} ${unit}`;
  }
}

/** A count as a short figure and its unit: 1840000 is "1.84" and "M". */
export function compactCount(value: number): { figure: string; unit: "" | "k" | "M" | "B" } {
  const steps = [
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "k"],
  ] as const;
  const step = steps.find(([size]) => Math.abs(value) >= size);
  if (!step) return { figure: String(Number(value.toPrecision(3))), unit: "" };
  return { figure: String(Number((value / step[0]).toPrecision(3))), unit: step[1] };
}

/**
 * The totals of a per-model series response, one row per model with the Controller's share of the
 * fleet total, and the fleet total (the series without a label). Nothing is added up here.
 */
export function modelTotals(data: MetricsSeriesResponse | null): {
  rows: { alias: string; total: number; share: number | null }[];
  sum: number;
  /** The lowest share of the range any series has data for (1 is complete). */
  coverage: number;
} {
  const all = data?.series ?? [];
  const series = all.flatMap((item) =>
    item.labels.alias && item.summary.total != null
      ? [
          {
            alias: item.labels.alias,
            total: Number(item.summary.total),
            share: item.summary.share == null ? null : Number(item.summary.share),
            coverage: item.summary.coverage,
          },
        ]
      : [],
  );
  const fleet = all.find((item) => !item.labels.alias)?.summary.total;
  return {
    rows: series.map(({ alias, total, share }) => ({ alias, total, share })),
    sum: fleet == null ? 0 : Number(fleet),
    coverage: series.reduce((lowest, item) => Math.min(lowest, item.coverage), 1),
  };
}

/** One alias's series in a response, with the response that carries its spec. */
export type AliasReading = {
  data: MetricsSeriesResponse;
  series: MetricsSeriesResponse["series"][number];
};

export function aliasSeries(
  data: MetricsSeriesResponse | null,
  alias: string | null,
): AliasReading | null {
  const series = data?.series.find((item) => item.labels.alias === alias);
  return data && series ? { data, series } : null;
}

/**
 * A workload's speed: the first read, in the order given, that has a latest value for the alias. Callers
 * pass the engine's measured throughput, then the gateway's estimate, then requests, so the figure
 * is the best the Controller has; none of them is computed here.
 */
export function leadReading(
  reads: readonly (MetricsSeriesResponse | null)[],
  alias: string | null,
): AliasReading | null {
  for (const read of reads) {
    const reading = aliasSeries(read, alias);
    if (reading?.series.summary.latest != null) return reading;
  }
  return null;
}

/** The reading's points inside the last `seconds` of its range, as chart points. */
export function windowPoints(
  reading: AliasReading,
  seconds: number,
): { t: number; v: number | null }[] {
  const end = Number(reading.data.end);
  return reading.series.points
    .filter((point) => Number(point.timestamp) >= end - seconds)
    .map((point) => ({
      t: Number(point.timestamp),
      v: point.value === null ? null : Number(point.value),
    }));
}

/**
 * The Controller's fleet line of a response (the series without a label, which sums the others) and how many
 * per-Spark series have a latest value behind it. Nothing is summed here.
 */
export function fleetLine(data: MetricsSeriesResponse | null): {
  data: MetricsSeriesResponse;
  summary: MetricsSeriesResponse["series"][number]["summary"];
  reporting: number;
} | null {
  const line = data?.series.find((item) => Object.keys(item.labels).length === 0);
  if (!data || !line || line.summary.latest == null) return null;
  return {
    data,
    summary: line.summary,
    reporting: data.series.filter((item) => item !== line && item.summary.latest != null).length,
  };
}
