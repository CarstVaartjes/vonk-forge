import "./fleet-pulse.css";
import type { ReactNode } from "react";
import type { MetricsSeriesResponse, VisualFleetNode } from "../api/types";
import { coverageText, fleetMemory, fleetPower, formatBytes } from "../lib/fleet";
import { compactCount, modelTotals } from "../lib/metric-units";
import { GPU_ONLY_CAPTION } from "./energy-card";
import { Estimate } from "./estimate";
import { MemoryIngot } from "./memory-ingot";

const NOT_REPORTED = "Not reported";

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
 * Five fleet figures in one band. Each is a Controller reading or a plain sum of Spark readings (GPU
 * power now, unified memory) or the Controller's fleet total (tokens); a figure with no reading says
 * Not reported. The engine's output speed and GPU energy wait for their metrics.
 */
export function FleetPulse({
  tokens,
  nodes,
}: {
  /** The gateway's generated tokens per model over 24 hours, as read by the page. */
  tokens: MetricsSeriesResponse | null;
  nodes: VisualFleetNode[];
}) {
  const online = nodes.filter((node) => node.connection.online_state === "online").length;
  const power = fleetPower(nodes);
  const memory = fleetMemory(nodes);
  const { rows, sum } = modelTotals(tokens);
  const generated = rows.length ? compactCount(sum) : null;
  return (
    <div className="fleet-pulse card">
      <Tile label="Output now" figure={NOT_REPORTED} caption="decode, measured at the engine" />
      <Tile
        label="GPU power"
        figure={power ? power.watts.toFixed(1) : NOT_REPORTED}
        unit={power ? "W" : undefined}
        caption={power ? coverageText(power.reporting, online) : undefined}
      />
      <Tile
        label="Unified memory"
        figure={memory ? formatBytes(memory.split.usedBytes).replace(/ GiB$/, "") : NOT_REPORTED}
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
            NOT_REPORTED
          )
        }
        unit={generated ? `${generated.unit} generated`.trim() : undefined}
        caption={generated ? "counted by the gateway, all models" : undefined}
      />
      <Tile label="GPU energy, 24 h" figure={NOT_REPORTED} caption={GPU_ONLY_CAPTION} />
    </div>
  );
}
