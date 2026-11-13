import { useId } from "react";
import type { VisualFleetNode } from "../api/types";
import { HISTORY_SECONDS, type HistoryPoint } from "../hooks/use-telemetry-history";
import { NOT_REPORTED_TEXT, halfUp, liveSample } from "../lib/fleet";
import { GPU_READINGS, num } from "../lib/gpu-readings";
import { throttleReasons } from "../lib/throttle";
import { GPU_TEMP_SERIOUS_C, GPU_TEMP_WARN_C } from "../lib/gpu-thresholds";
import { ArcGauge } from "./chart/arc-gauge";
import { Sparkline } from "./chart/sparkline";

const [UTILISATION, TEMPERATURE, POWER, CLOCK] = GPU_READINGS;

function workloads(node: VisualFleetNode): string {
  const names = node.loaded.map((run) => run.alias || run.title).filter(Boolean);
  return names.length ? `loaded: ${[...new Set(names)].join(", ")}` : "no workload loaded";
}

/**
 * The GPU's present state: utilisation, temperature and clock as gauges, then power with its trace. Every
 * figure is the live sample as reported; a Spark that is not reporting live shows Not reported and no
 * arc. Energy has no source yet and says so.
 */
export function SparkGpuNow({
  node,
  points,
  now,
  color,
}: {
  node: VisualFleetNode;
  points: readonly HistoryPoint[];
  now: Date;
  color: string;
}) {
  const id = useId();
  const sample = liveSample(node);
  const read = (reading: typeof UTILISATION | typeof TEMPERATURE | typeof CLOCK) => {
    const value = sample ? reading.now(sample) : null;
    return value === null ? null : halfUp(value);
  };
  const clock = sample?.gpu_clock;
  const hardMax = num(node.gpu_clock_policy?.hard_max_mhz) ?? num(clock?.max_graphics_mhz);
  const profile = node.gpu_clock_desired;
  const reasons = clock?.throttle_reasons;
  const throttle = sample && reasons != null ? throttleReasons(reasons) : null;
  const power = sample ? POWER.now(sample) : null;
  const end = now.getTime() / 1000;
  return (
    <section className="card spark-panel-card" aria-labelledby={id}>
      <header>
        <h2 id={id}>GPU now</h2>
      </header>
      <div className="spark-gauges">
        <ArcGauge
          title="Utilisation"
          value={read(UTILISATION)}
          unit="%"
          max={100}
          captions={[workloads(node)]}
          color={color}
        />
        <ArcGauge
          title="Temperature"
          value={read(TEMPERATURE)}
          unit="°C"
          max={100}
          ticks={[
            { value: GPU_TEMP_WARN_C, tone: "warn" },
            { value: GPU_TEMP_SERIOUS_C, tone: "bad" },
          ]}
          captions={[
            `warns at ${GPU_TEMP_WARN_C}\u00a0°C, serious at ${GPU_TEMP_SERIOUS_C}\u00a0°C`,
          ]}
          color={color}
        />
        <ArcGauge
          title="Clock"
          value={read(CLOCK)}
          unit="MHz"
          max={hardMax}
          ticks={profile ? [{ value: Number(profile.max_mhz), tone: "limit" }] : []}
          captions={[
            ...(profile ? [`profile range ${profile.min_mhz}–${profile.max_mhz} MHz`] : []),
            ...(throttle ? [`throttle: ${throttle.words}`] : []),
          ]}
          color={color}
        />
      </div>
      <div className="spark-power">
        <div>
          <span>GPU power</span>
          <strong>{power === null ? NOT_REPORTED_TEXT : `${power.toFixed(1)} W`}</strong>
        </div>
        <Sparkline
          points={points.map((point) => ({ t: point.t, v: point.power }))}
          start={end - HISTORY_SECONDS}
          end={end}
          color={color}
          label="GPU power, last 30 minutes"
        />
        <div>
          <span>GPU energy, 24 h</span>
          <strong>{NOT_REPORTED_TEXT}</strong>
        </div>
      </div>
    </section>
  );
}
