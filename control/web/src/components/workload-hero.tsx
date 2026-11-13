import type { MetricsSeriesResponse } from "../api/types";
import { RunState } from "../api/vocabulary.generated";
import { formatValue, UNITS } from "../lib/metric-units";
import type { WorkloadGroup } from "../lib/fleet";
import { AreaChart } from "./chart/area-chart";
import { Estimate, EstimateTag } from "./estimate";
import { StatusPill, type StatusTone } from "./status-pill";

const NOT_REPORTED = "Not reported";
const WINDOW_SECONDS = 1800;

/** The run's state word (a RunState, or `degraded` / `state unknown` from `runStateWord`) as a tone. */
const STATE_TONES: Record<string, StatusTone> = {
  [RunState.RUNNING]: "ok",
  [RunState.FAILED]: "bad",
  [RunState.LOST]: "bad",
  degraded: "warn",
  "state unknown": "neutral",
};

/** The slots the engine will fill once it reports them (M6); until then they read Not reported. */
const ENGINE_FIGURES = [
  "Prefill",
  "First token",
  "KV cache in use",
  "Prompt cache hits",
  "Draft tokens accepted",
] as const;

function seriesOf(data: MetricsSeriesResponse | null, alias: string | null) {
  const series = data?.series.find((item) => item.labels.alias === alias);
  return data && series ? { data, series } : null;
}

/**
 * One workload: alias, title and state, its live speed as the hero number with the last 30 minutes
 * behind it, and the figures beside it. The hero is the gateway's generated tokens per second (shown with
 * "≈" while the Controller calls it an estimate), else its requests per second, else Not reported. Every
 * number is the Controller's latest reading; the browser computes none.
 */
export function WorkloadHero({
  unit,
  tokens,
  requests,
  live,
}: {
  unit: WorkloadGroup;
  tokens: MetricsSeriesResponse | null;
  requests: MetricsSeriesResponse | null;
  live: boolean;
}) {
  const tokenSeries = seriesOf(tokens, unit.alias);
  const requestSeries = seriesOf(requests, unit.alias);
  const hero = tokenSeries?.series.summary.latest != null ? tokenSeries : requestSeries;
  const spec = hero?.data.spec;
  const latest = hero?.series.summary.latest;
  const symbol = spec ? UNITS[spec.unit].symbol : "";
  const value = latest == null ? null : Number(latest);
  const requestRate = requestSeries?.series.summary.latest;
  const end = hero ? Number(hero.data.end) : 0;
  return (
    <div className="workload-hero">
      <header>
        {unit.alias && <code className="alias">{unit.alias}</code>}
        <h2>{unit.title}</h2>
        <StatusPill tone={STATE_TONES[unit.state] ?? "info"}>{unit.state}</StatusPill>
        <span className="unit-facts">
          {unit.nodes.length} {unit.nodes.length === 1 ? "Spark" : "Sparks"}
        </span>
      </header>
      <div className="hero-body">
        <div className="hero-read">
          {spec && value !== null ? (
            <>
              <p className="hero-num">
                <Estimate basis={spec.basis}>{formatValue(value, "")}</Estimate>
                <small>{symbol}</small>
              </p>
              <p className="hero-caption">
                {value === 0 && unit.state === RunState.RUNNING
                  ? "idle, model loaded"
                  : `${spec.title} · last 30 minutes`}
                <EstimateTag basis={spec.basis} />
              </p>
            </>
          ) : (
            <p className="hero-num hero-missing">{NOT_REPORTED}</p>
          )}
          <dl className="hero-figures">
            <div>
              <dt>Requests</dt>
              <dd>
                {requestSeries && requestRate != null ? (
                  <>
                    <Estimate basis={requestSeries.data.spec.basis}>
                      {formatValue(Number(requestRate), "")}
                    </Estimate>
                    <small>{UNITS[requestSeries.data.spec.unit].symbol}</small>
                  </>
                ) : (
                  NOT_REPORTED
                )}
              </dd>
            </div>
            {ENGINE_FIGURES.map((name) => (
              <div key={name}>
                <dt>{name}</dt>
                <dd>{NOT_REPORTED}</dd>
              </div>
            ))}
          </dl>
        </div>
        {hero && spec && value !== null && (
          <div className="hero-chart">
            <AreaChart
              title={spec.title}
              label={`${unit.title} ${spec.title}, last 30 minutes, now ${formatValue(value, symbol)}`}
              series={[
                {
                  key: "hero",
                  name: spec.title,
                  color: "var(--model-ink)",
                  points: hero.series.points
                    .filter((point) => Number(point.timestamp) >= end - WINDOW_SECONDS)
                    .map((point) => ({
                      t: Number(point.timestamp),
                      v: point.value === null ? null : Number(point.value),
                    })),
                },
              ]}
              start={end - WINDOW_SECONDS}
              end={end}
              format={(number) => formatValue(number, symbol)}
              live={live}
            />
          </div>
        )}
      </div>
    </div>
  );
}
