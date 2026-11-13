import { useEffect, useState } from "react";
import type {
  ControlApi,
  MetricsSeriesResponse,
  TelemetryPoint,
  TelemetryState,
} from "../api/types";
import { telemetryFreshnessAt } from "../lib/fleet";

/** What the live traces keep: the last 30 minutes, one sample per 10 s bucket (at most 180 points). */
export const HISTORY_SECONDS = 1800;
const BUCKET_SECONDS = 10;
const MAX_POINTS = HISTORY_SECONDS / BUCKET_SECONDS;
/** A silence longer than this between two samples is a gap in the trace, not a straight line. */
const GAP_SECONDS = 20;

/** One moment of a Spark's telemetry, each reading as the Spark reported it; null is not reported. */
export type HistoryPoint = {
  t: number;
  utilization: number | null;
  temperature: number | null;
  power: number | null;
  clockMhz: number | null;
};

const EMPTY = { utilization: null, temperature: null, power: null, clockMhz: null } as const;
const bucket = (t: number) => Math.floor(t / BUCKET_SECONDS);

function pointOf(sample: TelemetryPoint): HistoryPoint | null {
  const t = Date.parse(sample.observed_at) / 1000;
  if (!Number.isFinite(t)) return null;
  const reading = (value: unknown) =>
    value === null || value === undefined ? null : Number(value);
  return {
    t,
    utilization: reading(sample.gpu_utilization_percent),
    temperature: reading(sample.gpu_temperature_c),
    power: reading(sample.gpu_power_watts),
    clockMhz: reading(sample.gpu_clock?.graphics_mhz),
  };
}

/** Sorted, within 30 minutes of the newest point, one per bucket (the newest wins), at most 180. */
function tidy(points: HistoryPoint[]): HistoryPoint[] {
  const newest = points.reduce((max, point) => Math.max(max, point.t), 0);
  const kept: HistoryPoint[] = [];
  for (const point of [...points].sort((a, b) => a.t - b.t)) {
    if (point.t < newest - HISTORY_SECONDS) continue;
    const last = kept.at(-1);
    if (last && bucket(last.t) === bucket(point.t)) kept[kept.length - 1] = point;
    else kept.push(point);
  }
  return kept.slice(-MAX_POINTS);
}

/** Add one stream sample. A long silence leaves a marker point of nulls so no line is drawn across it. */
export function appendSample(points: HistoryPoint[], sample: TelemetryPoint): HistoryPoint[] {
  const next = pointOf(sample);
  const last = points.at(-1);
  if (!next || (last && next.t < last.t)) return points;
  const gap =
    last && next.t - last.t > GAP_SECONDS ? [{ t: last.t + BUCKET_SECONDS, ...EMPTY }] : [];
  return tidy([...points, ...gap, next]);
}

/** The Controller's last hour of the two readings it keeps, ahead of the live points. */
export function seedPoints(
  utilization: MetricsSeriesResponse,
  temperature: MetricsSeriesResponse,
  live: HistoryPoint[],
): HistoryPoint[] {
  const byTime = new Map<number, HistoryPoint>();
  const read = (
    data: MetricsSeriesResponse,
    patch: (point: HistoryPoint, value: number | null) => HistoryPoint,
  ) => {
    for (const point of data.series[0]?.points ?? []) {
      const t = Number(point.timestamp);
      const value = point.value === null ? null : Number(point.value);
      byTime.set(t, patch(byTime.get(t) ?? { t, ...EMPTY }, value));
    }
  };
  read(utilization, (point, value) => ({ ...point, utilization: value }));
  read(temperature, (point, value) => ({ ...point, temperature: value }));
  const firstLive = live[0]?.t ?? Number.POSITIVE_INFINITY;
  return tidy([...[...byTime.values()].filter((point) => point.t < firstLive), ...live]);
}

/**
 * The Spark's live history for its traces: seeded once from the Controller's last hour, then fed by the
 * fleet stream's telemetry. `live` is true only while the newest sample is fresh. The hook keeps the
 * readings as reported and computes nothing from them.
 */
export function useTelemetryHistory({
  api,
  nodeId,
  telemetry,
  now,
}: {
  api: Pick<ControlApi, "metricsSeries">;
  nodeId: string;
  telemetry: TelemetryState | null | undefined;
  now: Date;
}): { points: HistoryPoint[]; live: boolean } {
  const [points, setPoints] = useState<HistoryPoint[]>([]);
  useEffect(() => {
    setPoints([]);
    const controller = new AbortController();
    // A failed seed leaves the live points to build the trace on their own.
    Promise.all([
      api.metricsSeries("gpu_utilization", "1h", nodeId, controller.signal),
      api.metricsSeries("gpu_temperature", "1h", nodeId, controller.signal),
    ]).then(
      ([utilization, temperature]) => {
        if (!controller.signal.aborted)
          setPoints((live) => seedPoints(utilization, temperature, live));
      },
      () => undefined,
    );
    return () => controller.abort();
  }, [api, nodeId]);
  const sample = telemetry?.sample;
  useEffect(() => {
    if (sample) setPoints((current) => appendSample(current, sample));
  }, [sample]);
  return { points, live: telemetryFreshnessAt(sample?.observed_at, now) === "live" };
}
