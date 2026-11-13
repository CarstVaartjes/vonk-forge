import type { HistoryPoint } from "../hooks/use-telemetry-history";
import { type Sample, num } from "./gpu-readings";

/**
 * The board sensors the agent reads without privilege: title and unit, how to read each from the live
 * sample (null is not reported) and from a history point. Fabric traffic and disk I/O are counters the
 * Controller has to turn into rates, so they are not here.
 */
export const SENSOR_READINGS = [
  {
    title: "Hottest board sensor",
    unit: "°C",
    now: (s: Sample) => num(s.board_temperature_max_c),
    past: (p: HistoryPoint) => p.boardC,
  },
  {
    title: "NVMe",
    unit: "°C",
    now: (s: Sample) => num(s.nvme_temperature_c),
    past: (p: HistoryPoint) => p.nvmeC,
  },
  {
    title: "Network card",
    unit: "°C",
    now: (s: Sample) => num(s.nic_temperature_c),
    past: (p: HistoryPoint) => p.nicC,
  },
  {
    title: "Fastest fan",
    unit: "RPM",
    now: (s: Sample) => num(s.fan_rpm_max),
    past: (p: HistoryPoint) => p.fanRpm,
  },
] as const;
