import "./workload-row.css";
import type { MouseEvent, ReactNode } from "react";
import type { MetricsSeriesResponse } from "../api/types";
import type { WorkloadReads } from "../hooks/use-workload-reads";
import {
  formatBytes,
  NOT_REPORTED_TEXT,
  nodeDisplayName,
  nodeMemorySplit,
  type WorkloadGroup,
} from "../lib/fleet";
import {
  aliasSeries,
  compactCount,
  formatValue,
  leadReading,
  modelTotals,
  UNITS,
  windowPoints,
} from "../lib/metric-units";
import { Sparkline } from "./chart/sparkline";
import { sparkColor } from "./chart/series";
import { Estimate } from "./estimate";
import { StatusPill } from "./status-pill";

const WINDOW_SECONDS = 1800;

/**
 * One workload on one line: alias, title and state, the Sparks that carry it, its speed now with the
 * last 30 minutes, requests, tokens in the last 24 hours and the memory its Sparks hold for models.
 * Every figure is the Controller's reading or the Spark's own sample; one that is missing says Not
 * reported. A workload still loading has no speed yet and points to Activity. The title is the button
 * that opens `children`, the full unit.
 */
export function WorkloadRow({
  unit,
  reads,
  day,
  order,
  open,
  onToggle,
  onActivity,
  children,
}: {
  unit: WorkloadGroup;
  reads: WorkloadReads;
  /** The gateway's generated tokens per model over 24 hours. */
  day: MetricsSeriesResponse | null;
  /** Each Spark's place in the snapshot's node order, which fixes its colour. */
  order: ReadonlyMap<string, number>;
  open: boolean;
  onToggle(): void;
  onActivity?(event: MouseEvent<HTMLAnchorElement>): void;
  children?: ReactNode;
}) {
  const speed = leadReading([reads.decode, reads.tokens, reads.requests], unit.alias);
  const requests = aliasSeries(reads.requests, unit.alias);
  const tokens = modelTotals(day).rows.find((row) => row.alias === unit.alias);
  const requestRate = requests?.series.summary.latest;
  const speedNow = speed?.series.summary.latest;
  const speedSymbol = speed ? UNITS[speed.data.spec.unit].symbol : "";
  const end = speed ? Number(speed.data.end) : 0;
  const total = tokens && compactCount(tokens.total);
  return (
    <li className="workload-row card">
      <div className="row-grid">
        <div className="row-name">
          {unit.alias && <code className="unit-alias">{unit.alias}</code>}
          <h3>
            <button type="button" aria-expanded={open} onClick={onToggle}>
              {unit.title}
            </button>
          </h3>
          <StatusPill tone={unit.tone}>{unit.state}</StatusPill>
          <ul className="row-sparks" aria-label="Sparks">
            {unit.nodes.map((node) => (
              <li key={node.id}>
                <span
                  className="spark-key"
                  style={{ background: sparkColor(order.get(node.id) ?? 0) }}
                  aria-hidden="true"
                />
                {nodeDisplayName(node)}
              </li>
            ))}
          </ul>
        </div>
        {unit.loading ? (
          <p className="row-loading">
            Loading.{" "}
            <a href="/activity" onClick={onActivity}>
              Follow it in Activity
            </a>
          </p>
        ) : (
          <>
            <div className="row-speed">
              <span className="row-label">{speed?.data.spec.title ?? "Speed"}</span>
              {speed && speedNow != null ? (
                <>
                  <strong>
                    <Estimate basis={speed.data.spec.basis}>
                      {formatValue(Number(speedNow), "")}
                    </Estimate>
                    <small>{speedSymbol}</small>
                  </strong>
                  <Sparkline
                    points={windowPoints(speed, WINDOW_SECONDS)}
                    start={end - WINDOW_SECONDS}
                    end={end}
                    label={`${unit.title} ${speed.data.spec.title}, last 30 minutes`}
                  />
                </>
              ) : (
                <strong>{NOT_REPORTED_TEXT}</strong>
              )}
            </div>
            <div>
              <span className="row-label">Requests</span>
              <strong>
                {requests && requestRate != null ? (
                  <>
                    <Estimate basis={requests.data.spec.basis}>
                      {formatValue(Number(requestRate), "")}
                    </Estimate>
                    <small>{UNITS[requests.data.spec.unit].symbol}</small>
                  </>
                ) : (
                  NOT_REPORTED_TEXT
                )}
              </strong>
            </div>
            <div>
              <span className="row-label">Tokens, 24 h</span>
              <strong>
                {day && total ? (
                  <>
                    <Estimate basis={day.spec.basis}>{total.figure}</Estimate>
                    <small>{total.unit}</small>
                  </>
                ) : (
                  NOT_REPORTED_TEXT
                )}
              </strong>
            </div>
            <div>
              <span className="row-label">Memory held</span>
              <ul className="row-memory">
                {unit.nodes.map((node) => {
                  const models = nodeMemorySplit(node)?.modelsBytes;
                  return (
                    <li key={node.id}>
                      {unit.nodes.length > 1 && <small>{nodeDisplayName(node)} </small>}
                      <strong>{models == null ? NOT_REPORTED_TEXT : formatBytes(models)}</strong>
                    </li>
                  );
                })}
              </ul>
            </div>
          </>
        )}
      </div>
      {open && children}
    </li>
  );
}
