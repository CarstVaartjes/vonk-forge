import { useEffect, useState } from "react";
import type {
  ControlApi,
  MetricsSeriesResponse,
  TelemetryPoint,
  FleetTelemetryState,
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
  /** Memory the GPU driver reports for compute processes (the models), and the rest in use (system), in bytes. */
  modelsBytes: number | null;
  systemBytes: number | null;
  /** The board sensors: hottest board sensor, NVMe and network card in degrees Celsius, the fastest fan in rpm. */
  boardC: number | null;
  nvmeC: number | null;
  nicC: number | null;
  fanRpm: number | null;
};

const EMPTY = {
  utilization: null,
  temperature: null,
  power: null,
  clockMhz: null,
  modelsBytes: null,
  systemBytes: null,
  boardC: null,
  nvmeC: null,
  nicC: null,
  fanRpm: null,
} as const;
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
    modelsBytes: reading(sample.gpu_process_memory_bytes),
    systemBytes: reading(sample.system_memory_bytes),
    boardC: reading(sample.board_temperature_max_c),
    nvmeC: reading(sample.nvme_temperature_c),
    nicC: reading(sample.nic_temperature_c),
    fanRpm: reading(sample.fan_rpm_max),
  };
}

const isMarker = (point: HistoryPoint) =>
  point.utilization === null &&
  point.temperature === null &&
  point.power === null &&
  point.clockMhz === null &&
  point.modelsBytes === null &&
  point.systemBytes === null &&
  point.boardC === null &&
  point.nvmeC === null &&
  point.nicC === null &&
  point.fanRpm === null;

/**
 * Sorted, within 30 minutes of the newest point, one per bucket (the newest wins), at most 180. Where
 * two real points that end at or after `since` are more than `gapSeconds` apart, a marker point of
 * nulls goes between them, so no line is drawn across the silence.
 */
function tidy(points: HistoryPoint[], gapSeconds: number, since: number): HistoryPoint[] {
  const newest = points.reduce((max, point) => Math.max(max, point.t), 0);
  const kept: HistoryPoint[] = [];
  for (const point of [...points].sort((a, b) => a.t - b.t)) {
    if (point.t < newest - HISTORY_SECONDS) continue;
    const last = kept.at(-1);
    if (last && bucket(last.t) === bucket(point.t)) kept[kept.length - 1] = point;
    else {
      if (
        last &&
        point.t >= since &&
        point.t - last.t > gapSeconds &&
        !isMarker(last) &&
        !isMarker(point)
      )
        kept.push({ t: last.t + BUCKET_SECONDS, ...EMPTY });
      kept.push(point);
    }
  }
  return kept.slice(-MAX_POINTS);
}

/** Add one stream sample; a silence since the previous one leaves a gap marker. */
export function appendSample(points: HistoryPoint[], sample: TelemetryPoint): HistoryPoint[] {
  const next = pointOf(sample);
  const last = points.at(-1);
  if (!next || (last && next.t < last.t)) return points;
  return tidy([...points, next], GAP_SECONDS, next.t);
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
  // Seed samples come every step_seconds; a hole of more than two steps is a gap, at the seam too.
  const gap = Math.max(GAP_SECONDS, 2 * Number(utilization.step_seconds));
  const seed = tidy(
    [...byTime.values()].filter((point) => point.t < firstLive),
    gap,
    Number.NEGATIVE_INFINITY,
  );
  return tidy([...seed, ...live], gap, firstLive);
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
  telemetry: FleetTelemetryState | null | undefined;
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
