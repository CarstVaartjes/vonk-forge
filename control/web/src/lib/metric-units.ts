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
  data: MetricsSeriesResponse | null | undefined,
  alias: string | null,
): AliasReading | null {
  const series = data?.series.find((item) => item.labels.alias === alias);
  return data && series ? { data, series } : null;
}

/**
 * A workload's lead figure, the same source for every engine: the gateway's generated tokens per second,
 * else its requests per second. `pending` is true while a read is still out and none has a value yet.
 * Nothing is computed here; the engine's own decode speed is shown in the engine panel.
 */
export function leadReading(
  reads: {
    tokens: MetricsSeriesResponse | null | undefined;
    requests: MetricsSeriesResponse | null | undefined;
  },
  alias: string | null,
): { reading: AliasReading | null; pending: boolean } {
  const order = [reads.tokens, reads.requests];
  for (const read of order) {
    const reading = aliasSeries(read, alias);
    if (reading?.series.summary.latest != null) return { reading, pending: false };
  }
  return { reading: null, pending: order.includes(undefined) };
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
 * One figure of the Controller's fleet line (the series without a label, which sums the others): its
 * latest value or its range total, in the metric's display unit, with its basis and hint and how many
 * per-Spark series have a latest value behind it. Nothing is summed here; null when the line or the field
 * is missing.
 */
export function fleetFigure(
  data: MetricsSeriesResponse | null | undefined,
  field: "latest" | "total",
) {
  const line = data?.series.find((item) => Object.keys(item.labels).length === 0);
  const raw = line?.summary[field];
  if (!data || !line || raw == null) return null;
  const { symbol, scale } = UNITS[data.spec.unit];
  return {
    value: Number(raw) / scale,
    symbol,
    basis: data.spec.basis,
    hint: data.spec.hint,
    reporting: data.series.filter((item) => item !== line && item.summary.latest != null).length,
  };
}

/**
 * The engine's request queue for an alias: "3 running · 1 waiting", only the parts the engine reports.
 * undefined while either read is out (nothing shown), null when the engine reports neither.
 */
export function queueText(
  running: MetricsSeriesResponse | null | undefined,
  waiting: MetricsSeriesResponse | null | undefined,
  alias: string | null,
): string | null | undefined {
  if (running === undefined || waiting === undefined) return undefined;
  const count = (data: MetricsSeriesResponse | null) => {
    const latest = aliasSeries(data, alias)?.series.summary.latest;
    return latest == null ? null : formatValue(Number(latest), "");
  };
  const [busy, queued] = [count(running), count(waiting)];
  const parts = [
    ...(busy === null ? [] : [`${busy} running`]),
    ...(queued === null ? [] : [`${queued} waiting`]),
  ];
  return parts.length ? parts.join(" · ") : null;
}
