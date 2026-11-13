import type { HistoryPoint } from "../hooks/use-telemetry-history";
import { liveSample } from "./fleet";

export type Sample = NonNullable<ReturnType<typeof liveSample>>;
export const num = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
/** One reading: its title and unit, how to read it from the live sample (null is not reported) and from a history point. */
export const GPU_READINGS = [
  {
    title: "GPU",
    unit: "%",
    fixedRange: true,
    now: (s: Sample) => num(s.gpu_utilization_percent),
    past: (p: HistoryPoint) => p.utilization,
  },
  {
    title: "GPU temperature",
    unit: "°C",
    now: (s: Sample) => num(s.gpu_temperature_c),
    past: (p: HistoryPoint) => p.temperature,
  },
  {
    title: "GPU power",
    unit: "W",
    digits: 1,
    now: (s: Sample) => num(s.gpu_power_watts == null ? null : Number(s.gpu_power_watts)),
    past: (p: HistoryPoint) => p.power,
  },
  {
    title: "GPU clock",
    unit: "MHz",
    now: (s: Sample) => num(s.gpu_clock?.graphics_mhz),
    past: (p: HistoryPoint) => p.clockMhz,
  },
] as const;
