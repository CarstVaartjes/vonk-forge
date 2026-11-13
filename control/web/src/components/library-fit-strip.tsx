import "./library-fit-strip.css";
import type { VisualFleetSnapshot } from "../api/types";
import {
  NOT_REPORTED_TEXT,
  fleetMemory,
  formatBytes,
  freeMemoryText,
  summarizeFleet,
} from "../lib/fleet";
import { MemoryIngot } from "./memory-ingot";

/**
 * What you can load right now, above the list: how many Sparks, their unified memory in total and what
 * is free (the Controller's status-line wording), with the fleet's memory bar. `fleet` is undefined
 * while it loads (nothing is drawn) and `failed` says the read failed (every figure is Not reported).
 */
export function LibraryFitStrip({
  fleet,
  failed,
}: {
  fleet: VisualFleetSnapshot | undefined;
  failed: boolean;
}) {
  if (!fleet && !failed) return null;
  const summary = summarizeFleet(fleet);
  const memory = fleet ? fleetMemory(fleet.nodes) : null;
  const free = summary ? freeMemoryText(summary) : null;
  return (
    <section className="library-fit card" aria-label="Your fleet">
      <p>
        <strong>
          {summary
            ? `${summary.enrolled} ${summary.enrolled === 1 ? "Spark" : "Sparks"}`
            : NOT_REPORTED_TEXT}
        </strong>
        <span>
          {memory ? `${formatBytes(memory.split.totalBytes)} unified memory` : NOT_REPORTED_TEXT}
        </span>
        <span>{free ?? NOT_REPORTED_TEXT}</span>
      </p>
      {memory && (
        <MemoryIngot label="Fleet unified memory" memory={memory.split} size="small" warn={false} />
      )}
    </section>
  );
}
