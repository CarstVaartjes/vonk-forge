import type {TelemetryHistory, TelemetryMetrics} from "../api/types";

export function telemetryMetrics(
  observedAt = "2026-08-15T11:59:58Z",
): TelemetryMetrics {
  return {
    schema_version: 2,
    series: [],
    capabilities: [],
    runtimes: [],
    workloads: [],
    provenance: {
      collector: "spark-agent",
      collector_version: "fixture-2",
      host_uptime_seconds: 7200,
      source_observed_at: observedAt,
    },
  };
}

export function historyMetadata(
  start: string,
  end: string,
  resolution: TelemetryHistory["resolution"],
  pointCount: number,
): TelemetryHistory["metadata"] {
  return {
    requested_start: start,
    requested_end: end,
    actual_start: pointCount > 0 ? start : null,
    actual_end: pointCount > 0 ? end : null,
    requested_resolution: resolution,
    actual_resolution: resolution,
    timezone: "UTC",
    point_count: pointCount,
    coverage_seconds: pointCount > 0
      ? Math.max(0, (Date.parse(end) - Date.parse(start)) / 1000)
      : 0,
    gap_samples: 0,
    downsampled: false,
  };
}
