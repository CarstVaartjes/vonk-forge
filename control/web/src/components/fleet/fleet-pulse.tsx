import "./fleet-pulse.css";
import type { ReactNode } from "react";
import type { MetricsSeriesResponse, VisualFleetNode } from "../../api/types";
import { NOT_REPORTED_TEXT, coverageText, fleetMemory, formatBytes } from "../../lib/fleet";
import { compactCount, fleetFigure, formatValue, modelTotals } from "../../lib/metric-units";
import { GPU_ONLY_CAPTION } from "../ui/energy-card";
import { Estimate } from "../ui/estimate";
import { MemoryIngot } from "../ui/memory-ingot";

/** A figure with no reading is a muted dash with a plain reason under it, never a display-size word. */
function Tile({
  label,
  figure,
  unit,
  caption,
  unavailable,
  children,
}: {
  label: string;
  figure?: ReactNode;
  unit?: string | undefined;
  caption?: string | undefined;
  /** Why there is no figure; shown instead of the figure and caption. */
  unavailable?: string | undefined;
  children?: ReactNode;
}) {
  return (
    <section className="pulse-tile" aria-label={label}>
      <h2 className="pulse-label">{label}</h2>
      {unavailable !== undefined ? (
        <>
          <p className="pulse-fig is-muted" aria-hidden="true">
            —
          </p>
          {unavailable && <p className="pulse-caption">{unavailable}</p>}
        </>
      ) : (
        <>
          <p className="pulse-fig">
            {figure}
            {unit && <small>{unit}</small>}
          </p>
          {caption && <p className="pulse-caption">{caption}</p>}
        </>
      )}
      {children}
    </section>
  );
}

/** Still reading says nothing; a finished read with no series is a gateway problem; a series without a value is idle. */
const noReading = (series: MetricsSeriesResponse | null | undefined) =>
  series === undefined ? "" : series ? "No inference traffic" : "Gateway metrics unavailable";

/**
 * Five fleet figures in one band. Each is the Controller's fleet line or total of a metric (output speed,
 * GPU power, tokens, GPU energy) or a plain sum of the snapshot's memory samples; a figure with no
 * reading is a muted dash with the reason.
 */
export function FleetPulse({
  output,
  power,
  tokens,
  energy,
  nodes,
}: {
  /** The gateway's tokens per second over the last hour (their latest is "Output now"); the workload rows' read. */
  output: MetricsSeriesResponse | null | undefined;
  /** GPU power, gateway tokens (24 h) and GPU energy as the Controller serves them. */
  power: MetricsSeriesResponse | null;
  tokens: MetricsSeriesResponse | null;
  energy: MetricsSeriesResponse | null;
  nodes: VisualFleetNode[];
}) {
  const online = nodes.filter((node) => node.connection.online_state === "online").length;
  const speed = fleetFigure(output, "latest");
  const watts = fleetFigure(power, "latest");
  const gpuEnergy = fleetFigure(energy, "total");
  const memory = fleetMemory(nodes);
  const { rows, sum } = modelTotals(tokens);
  const generated = rows.length ? compactCount(sum) : null;
  return (
    <div className="fleet-pulse card">
      <Tile
        label="Output now"
        figure={speed ? formatValue(speed.value, "") : undefined}
        unit={speed?.symbol}
        caption="generated, counted by the gateway"
        unavailable={speed ? undefined : noReading(output)}
      />
      <Tile
        label="GPU power"
        figure={watts?.value.toFixed(1)}
        unit={watts?.symbol}
        caption={watts ? coverageText(watts.reporting, online) : undefined}
        unavailable={watts ? undefined : NOT_REPORTED_TEXT}
      />
      <Tile
        label="Unified memory"
        figure={memory ? formatBytes(memory.split.usedBytes).replace(/ GiB$/, "") : undefined}
        unavailable={memory ? undefined : NOT_REPORTED_TEXT}
        unit={memory ? `GiB of ${formatBytes(memory.split.totalBytes)}` : undefined}
        caption={
          memory
            ? memory.split.modelsBytes !== null
              ? `${formatBytes(memory.split.modelsBytes)} in models`
              : coverageText(memory.reporting, online)
            : undefined
        }
      >
        {memory && (
          <MemoryIngot
            label="Fleet unified memory"
            memory={memory.split}
            size="small"
            warn={false}
          />
        )}
      </Tile>
      <Tile
        label="Tokens, 24 h"
        figure={
          generated && tokens ? (
            <Estimate basis={tokens.spec.basis}>{generated.figure}</Estimate>
          ) : undefined
        }
        unavailable={generated && tokens ? undefined : noReading(tokens)}
        unit={generated ? `${generated.unit} generated`.trim() : undefined}
        caption={generated ? "counted by the gateway, all models" : undefined}
      />
      <Tile
        label="GPU energy, 24 h"
        figure={
          gpuEnergy ? (
            <Estimate basis={gpuEnergy.basis}>
              {formatValue(gpuEnergy.value, gpuEnergy.symbol)}
            </Estimate>
          ) : undefined
        }
        caption={GPU_ONLY_CAPTION}
        unavailable={gpuEnergy ? undefined : NOT_REPORTED_TEXT}
      />
    </div>
  );
}
