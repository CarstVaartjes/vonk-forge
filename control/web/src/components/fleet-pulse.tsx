import "./fleet-pulse.css";
import type { ReactNode } from "react";
import type { MetricsSeriesResponse, VisualFleetNode } from "../api/types";
import { NOT_REPORTED_TEXT, coverageText, fleetMemory, formatBytes } from "../lib/fleet";
import { compactCount, fleetFigure, formatValue, modelTotals } from "../lib/metric-units";
import { GPU_ONLY_CAPTION } from "./energy-card";
import { Estimate } from "./estimate";
import { MemoryIngot } from "./memory-ingot";

function Tile({
  label,
  figure,
  unit,
  caption,
  children,
}: {
  label: string;
  figure: ReactNode;
  unit?: string;
  caption?: string;
  children?: ReactNode;
}) {
  return (
    <section className="pulse-tile" aria-label={label}>
      <h2 className="pulse-label">{label}</h2>
      <p className="pulse-fig">
        {figure}
        {unit && <small>{unit}</small>}
      </p>
      {caption && <p className="pulse-caption">{caption}</p>}
      {children}
    </section>
  );
}

/**
 * Five fleet figures in one band. Each is the Controller's fleet line or total of a metric (output speed,
 * GPU power, tokens, GPU energy) or a plain sum of the snapshot's memory samples; a figure with no
 * reading says Not reported.
 */
export function FleetPulse({
  decode,
  power,
  tokens,
  energy,
  nodes,
}: {
  /** The engine's decode throughput, GPU power, gateway tokens and GPU energy as the Controller serves them. */
  decode: MetricsSeriesResponse | null | undefined;
  power: MetricsSeriesResponse | null;
  tokens: MetricsSeriesResponse | null;
  energy: MetricsSeriesResponse | null;
  nodes: VisualFleetNode[];
}) {
  const online = nodes.filter((node) => node.connection.online_state === "online").length;
  const speed = fleetFigure(decode, "latest");
  const watts = fleetFigure(power, "latest");
  const gpuEnergy = fleetFigure(energy, "total");
  const memory = fleetMemory(nodes);
  const { rows, sum } = modelTotals(tokens);
  const generated = rows.length ? compactCount(sum) : null;
  return (
    <div className="fleet-pulse card">
      <Tile
        label="Output now"
        figure={speed ? formatValue(speed.value, "") : NOT_REPORTED_TEXT}
        unit={speed?.symbol}
        caption="decode, measured at the engine"
      />
      <Tile
        label="GPU power"
        figure={watts ? watts.value.toFixed(1) : NOT_REPORTED_TEXT}
        unit={watts?.symbol}
        caption={watts ? coverageText(watts.reporting, online) : undefined}
      />
      <Tile
        label="Unified memory"
        figure={
          memory ? formatBytes(memory.split.usedBytes).replace(/ GiB$/, "") : NOT_REPORTED_TEXT
        }
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
          ) : (
            NOT_REPORTED_TEXT
          )
        }
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
          ) : (
            NOT_REPORTED_TEXT
          )
        }
        caption={GPU_ONLY_CAPTION}
      />
    </div>
  );
}
