import type {TelemetryHistory, TelemetryHistoryPoint, TelemetryMetricSummary, TelemetrySeries, TelemetryScope} from "../api/types";

/**
 * The Controller keeps history self describing. These dimensions are the
 * identity of one metric stream; a key alone is insufficient when a Spark
 * reports multiple devices, interfaces, processes, or runs.
 */
export type TelemetryMetricIdentity = Pick<TelemetrySeries, "key" | "scope" | "unit" | "device_id" | "process_id" | "process_name" | "interface_name" | "run_id">;

export type TelemetryHistoryValue = {
  count: number;
  minimum: number;
  mean: number;
  maximum: number;
};

const IDENTITY_FIELDS: (keyof TelemetryMetricIdentity)[] = [
  "key",
  "scope",
  "unit",
  "device_id",
  "process_id",
  "process_name",
  "interface_name",
  "run_id",
];

function normalizedString(value: string | null | undefined): string | null {
  return value ?? null;
}

function normalizedNumber(value: number | null | undefined): number | null {
  return value ?? null;
}

export function metricIdentity(value: TelemetryMetricIdentity): TelemetryMetricIdentity {
  return {
    key: value.key,
    scope: value.scope,
    unit: value.unit,
    device_id: normalizedString(value.device_id),
    process_id: normalizedNumber(value.process_id),
    process_name: normalizedString(value.process_name),
    interface_name: normalizedString(value.interface_name),
    run_id: normalizedString(value.run_id),
  };
}

export function metricIdentityKey(value: TelemetryMetricIdentity): string {
  const identity = metricIdentity(value);
  return IDENTITY_FIELDS.map(field => `${field}:${String(identity[field])}`).join("|");
}

export function sameMetricIdentity(left: TelemetryMetricIdentity, right: TelemetryMetricIdentity): boolean {
  const normalizedLeft = metricIdentity(left);
  const normalizedRight = metricIdentity(right);
  return IDENTITY_FIELDS.every(field => normalizedLeft[field] === normalizedRight[field]);
}

function finite(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function isRollupPoint(point: TelemetryHistoryPoint): point is Extract<TelemetryHistoryPoint, {resolution: string}> {
  return "resolution" in point;
}

export function historyPointTimestamp(point: TelemetryHistoryPoint): string {
  return isRollupPoint(point) ? point.bucket_end : point.observed_at;
}

function rawMetric(point: TelemetryHistoryPoint, identity: TelemetryMetricIdentity): TelemetrySeries | undefined {
  if (isRollupPoint(point)) return undefined;
  return point.metrics?.series.find(series => sameMetricIdentity(series, identity));
}

const TELEMETRY_SCOPES: readonly TelemetryScope[] = ["node", "accelerator", "memory", "storage", "network", "runtime", "workload", "service", "benchmark"];

function isTelemetryScope(value: string): value is TelemetryScope {
  return TELEMETRY_SCOPES.some(scope => scope === value);
}

function summaryIdentity(summary: TelemetryMetricSummary): TelemetryMetricIdentity | undefined {
  if (!summary.key || !summary.scope || !isTelemetryScope(summary.scope)) return undefined;
  return {
    key: summary.key,
    scope: summary.scope,
    unit: summary.unit,
    device_id: summary.device_id ?? null,
    process_id: summary.process_id ?? null,
    process_name: summary.process_name ?? null,
    interface_name: summary.interface_name ?? null,
    run_id: summary.run_id ?? null,
  };
}

function rollupMetric(point: TelemetryHistoryPoint, identity: TelemetryMetricIdentity): TelemetryMetricSummary | undefined {
  if (!isRollupPoint(point)) return undefined;
  return Object.values(point.metrics).find(summary => {
    const candidate = summaryIdentity(summary);
    return candidate !== undefined && sameMetricIdentity(candidate, identity);
  });
}

function pointValue(point: TelemetryHistoryPoint, identity: TelemetryMetricIdentity): TelemetryHistoryValue | null {
  const series = rawMetric(point, identity);
  if (series) {
    return series.support_status === "available" && finite(series.value)
      ? {count: 1, minimum: series.value, mean: series.value, maximum: series.value}
      : null;
  }
  const summary = rollupMetric(point, identity);
  return summary && finite(summary.mean) && finite(summary.minimum) && finite(summary.maximum)
    ? {count: summary.count, minimum: summary.minimum, mean: summary.mean, maximum: summary.maximum}
    : null;
}

export function historySeries(history: TelemetryHistory | undefined, identity: TelemetryMetricIdentity): (TelemetryHistoryValue | null)[] {
  return (history?.points ?? []).map(point => pointValue(point, identity));
}

export function historyValues(history: TelemetryHistory | undefined, identity: TelemetryMetricIdentity): (number | null)[] {
  return historySeries(history, identity).map(value => value?.mean ?? null);
}

export function historyLastObservedAt(history: TelemetryHistory | undefined, identity: TelemetryMetricIdentity): string | null {
  const points = history?.points ?? [];
  for (let index = points.length - 1; index >= 0; index -= 1) {
    if (pointValue(points[index]!, identity) !== null) return historyPointTimestamp(points[index]!);
  }
  return null;
}

/** A line needs two adjacent reported samples; isolated points stay gaps. */
export function hasContiguousHistory(values: readonly (number | null)[]): boolean {
  let previous = false;
  for (const value of values) {
    const current = value !== null && Number.isFinite(value);
    if (current && previous) return true;
    previous = current;
  }
  return false;
}

export function historyIdentities(history: TelemetryHistory | undefined): TelemetryMetricIdentity[] {
  const identities = new Map<string, TelemetryMetricIdentity>();
  for (const point of history?.points ?? []) {
    if (isRollupPoint(point)) {
      for (const summary of Object.values(point.metrics)) {
        const identity = summaryIdentity(summary);
        if (!identity) continue;
        if (!identities.has(metricIdentityKey(identity))) identities.set(metricIdentityKey(identity), identity);
      }
      continue;
    }
    for (const series of point.metrics?.series ?? []) {
      if (series.support_status !== "available" || !finite(series.value)) continue;
      const identity = metricIdentity(series);
      if (!identities.has(metricIdentityKey(identity))) identities.set(metricIdentityKey(identity), identity);
    }
  }
  return [...identities.values()];
}
