import type { MetricSpec } from "../api/types";

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
