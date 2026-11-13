import type { MetricsSeriesResponse } from "../api/types";
import { aliasSeries, formatValue, leadReading, UNITS, windowPoints } from "../lib/metric-units";
import { NOT_REPORTED_TEXT, type WorkloadGroup } from "../lib/fleet";
import { AreaChart } from "./chart/area-chart";
import { Estimate, EstimateTag } from "./estimate";
import { ENGINE_FIGURES } from "../hooks/use-workload-reads";
import { StatusPill } from "./status-pill";

const WINDOW_SECONDS = 1800;

/** A workload's alias, title, state and Spark count: the header of a hero, and the whole strip of a second workload on the same Sparks. */
export function WorkloadHeader({ unit }: { unit: WorkloadGroup }) {
  return (
    <header>
      {unit.alias && <code className="unit-alias">{unit.alias}</code>}
      <h2>{unit.title}</h2>
      <StatusPill tone={unit.tone}>{unit.state}</StatusPill>
      <span className="unit-facts">
        {unit.nodes.length} {unit.nodes.length === 1 ? "Spark" : "Sparks"}
      </span>
    </header>
  );
}

/**
 * One workload: alias, title and state, its live speed as the hero number with the last 30 minutes
 * behind it, and the figures beside it. The hero is the engine's measured decode speed, else the gateway's
 * generated tokens per second, else its requests per second, else Not reported; "≈" shows while the
 * Controller calls the figure an estimate. Every number is the Controller's latest reading; the browser
 * computes none.
 */
export function WorkloadHero({
  unit,
  decode,
  tokens,
  requests,
  figures,
  live,
}: {
  unit: WorkloadGroup;
  decode: MetricsSeriesResponse | null;
  tokens: MetricsSeriesResponse | null;
  requests: MetricsSeriesResponse | null;
  figures: (MetricsSeriesResponse | null)[];
  live: boolean;
}) {
  const requestSeries = aliasSeries(requests, unit.alias);
  const hero = leadReading([decode, tokens, requests], unit.alias);
  const spec = hero?.data.spec;
  const latest = hero?.series.summary.latest;
  const symbol = spec ? UNITS[spec.unit].symbol : "";
  const value = latest == null ? null : Number(latest);
  const requestRate = requestSeries?.series.summary.latest;
  return (
    <div className="workload-hero">
      <WorkloadHeader unit={unit} />
      <div className="hero-body">
        <div className="hero-read">
          {spec && value !== null ? (
            <>
              <p className="hero-num">
                <Estimate basis={spec.basis}>{formatValue(value, "")}</Estimate>
                <small>{symbol}</small>
              </p>
              <p className="hero-caption">
                {value === 0 && unit.tone === "ok"
                  ? "idle, model loaded"
                  : `${spec.title} · last 30 minutes`}
                <EstimateTag basis={spec.basis} />
              </p>
            </>
          ) : (
            <p className="hero-num hero-missing">{NOT_REPORTED_TEXT}</p>
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
                  NOT_REPORTED_TEXT
                )}
              </dd>
            </div>
            {ENGINE_FIGURES.map(({ title }, index) => {
              const reading = aliasSeries(figures[index] ?? null, unit.alias);
              const latest = reading?.series.summary.latest;
              return (
                <div key={title}>
                  <dt>{title}</dt>
                  <dd>
                    {reading && latest != null ? (
                      <Estimate basis={reading.data.spec.basis}>
                        {formatValue(
                          Number(latest) / UNITS[reading.data.spec.unit].scale,
                          UNITS[reading.data.spec.unit].symbol,
                        )}
                      </Estimate>
                    ) : (
                      NOT_REPORTED_TEXT
                    )}
                  </dd>
                </div>
              );
            })}
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
                  points: windowPoints(hero, WINDOW_SECONDS),
                },
              ]}
              start={Number(hero.data.end) - WINDOW_SECONDS}
              end={Number(hero.data.end)}
              format={(number) => formatValue(number, symbol)}
              live={live}
            />
          </div>
        )}
      </div>
    </div>
  );
}
