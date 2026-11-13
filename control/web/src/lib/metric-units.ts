import type { MetricSpec, MetricsSeriesResponse } from "../api/types";

const GIB = 1024 ** 3;
// How the Controller's unit names read on the page; titles and basis come from the catalogue.
export const UNITS: Record<MetricSpec["unit"], { symbol: string; scale: number }> = {
  percent: { symbol: "%", scale: 1 },
  bytes: { symbol: "GiB", scale: GIB },
  celsius: { symbol: "°C", scale: 1 },
  watts: { symbol: "W", scale: 1 },
  watt_hours: { symbol: "Wh", scale: 1 },
  rpm: { symbol: "rpm", scale: 1 },
  per_second: { symbol: "/s", scale: 1 },
  tokens_per_second: { symbol: "tok/s", scale: 1 },
  seconds: { symbol: "s", scale: 1 },
  ratio: { symbol: "", scale: 1 },
  count: { symbol: "", scale: 1 },
};
export function formatValue(value: number, unit: string): string {
  const text = Math.abs(value) >= 100 ? value.toFixed(0) : value.toPrecision(3);
  return `${Number(text)} ${unit}`.trim();
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
 * The 24 h totals of a per-model series response, one per model (a series without an alias is not a
 * model and is left out), and their sum. Display arithmetic on the Controller's totals; it moves to the
 * Controller's summary with M3.
 */
export function modelTotals(data: MetricsSeriesResponse | null): {
  rows: { alias: string; total: number }[];
  sum: number;
  /** The lowest share of the range any series has data for (1 is complete). */
  coverage: number;
} {
  const series = (data?.series ?? []).flatMap((item) =>
    item.labels.alias && item.summary.total != null
      ? [
          {
            alias: item.labels.alias,
            total: Number(item.summary.total),
            coverage: item.summary.coverage,
          },
        ]
      : [],
  );
  return {
    rows: series.map(({ alias, total }) => ({ alias, total })),
    sum: series.reduce((sum, item) => sum + item.total, 0),
    coverage: series.reduce((lowest, item) => Math.min(lowest, item.coverage), 1),
  };
}
