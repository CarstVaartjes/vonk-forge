import { ENGINE_FIGURES, type WorkloadReads } from "../hooks/use-workload-reads";
import {
  aliasSeries,
  formatValue,
  leadReading,
  queueText,
  UNITS,
  windowPoints,
} from "../lib/metric-units";
import { NOT_REPORTED_TEXT, type WorkloadGroup } from "../lib/fleet";
import { AreaChart } from "./chart/area-chart";
import { Estimate, EstimateTag } from "./estimate";
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
 * generated tokens per second, else its requests per second, else Not reported (nothing while the reads are out); "≈" shows while the
 * Controller calls the figure an estimate. Every number is the Controller's latest reading; the browser
 * computes none.
 */
export function WorkloadHero({
  unit,
  reads,
  live,
}: {
  unit: WorkloadGroup;
  reads: WorkloadReads;
  live: boolean;
}) {
  const hero = leadReading([reads.decode, reads.tokens, reads.requests], unit.alias);
  const spec = hero?.data.spec;
  const latest = hero?.series.summary.latest;
  const symbol = spec ? UNITS[spec.unit].symbol : "";
  const value = latest == null ? null : Number(latest);
  const queue = queueText(reads.running, reads.waiting, unit.alias);
  // While the speed reads are out nothing is claimed; once they are in, no latest value is Not reported.
  const pending = !hero && [reads.decode, reads.tokens, reads.requests].includes(undefined);
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
            !pending && <p className="hero-num hero-missing">{NOT_REPORTED_TEXT}</p>
          )}
          <dl className="hero-figures">
            <div>
              <dt>Requests</dt>
              <dd>{queue === undefined ? null : (queue ?? NOT_REPORTED_TEXT)}</dd>
            </div>
            {ENGINE_FIGURES.map((metric, index) => {
              const spec = reads.specs.find((entry) => entry.metric === metric);
              if (!spec) return null;
              const read = reads.figures[index];
              const latest = aliasSeries(read, unit.alias)?.series.summary.latest;
              const { symbol, scale } = UNITS[spec.unit];
              return (
                <div key={metric}>
                  <dt>{spec.title}</dt>
                  <dd>
                    {read === undefined ? null : latest == null ? (
                      NOT_REPORTED_TEXT
                    ) : (
                      <Estimate basis={spec.basis}>
                        {formatValue(Number(latest) / scale, symbol)}
                      </Estimate>
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
