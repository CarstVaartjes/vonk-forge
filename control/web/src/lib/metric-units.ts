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
