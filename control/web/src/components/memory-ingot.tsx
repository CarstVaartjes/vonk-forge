import "./memory-ingot.css";
import { displayRatio, type WireNumber } from "../api/contract-numeric";
import { formatBytes, memoryLevel, type MemorySplit } from "../lib/fleet";
import { StatusPill } from "./status-pill";

const LOW_WORDS = { warn: "Under 8 GiB free", serious: "Under 4 GiB free" } as const;

/**
 * A Spark's unified memory as one bar: Models, System and Free, with the amounts in a legend. When the
 * Spark did not report the split it is Used and Free. The bar is a meter named by its full text; running
 * low on free memory adds a word and a shape, never only a colour.
 */
export function MemoryIngot({
  label,
  memory,
  size = "medium",
  warn = true,
}: {
  label: string;
  memory: MemorySplit | null;
  size?: "small" | "medium" | "large";
  /** The 8 / 4 GiB free-memory warning is one Spark's; a fleet sum passes false. */
  warn?: boolean;
}) {
  if (!memory) return <span>Not reported</span>;
  const split = memory.modelsBytes !== null && memory.systemBytes !== null;
  const parts: { key: string; word: string; bytes: WireNumber }[] = split
    ? [
        { key: "models", word: "Models", bytes: memory.modelsBytes as WireNumber },
        { key: "system", word: "System", bytes: memory.systemBytes as WireNumber },
        { key: "free", word: "Free", bytes: memory.freeBytes },
      ]
    : [
        { key: "used", word: "Used", bytes: memory.usedBytes },
        { key: "free", word: "Free", bytes: memory.freeBytes },
      ];
  const text = `${parts.map((part) => `${part.word} ${formatBytes(part.bytes)}`).join(", ")} of ${formatBytes(memory.totalBytes)}`;
  const level = warn ? memoryLevel(memory.freeBytes) : "ok";
  return (
    <div className="memory-ingot">
      <div
        className={`ingot ingot-${size}`}
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(100 * displayRatio(memory.usedBytes, memory.totalBytes))}
        aria-valuetext={text}
      >
        {parts.map((part) => (
          <i
            key={part.key}
            className={`ingot-${part.key}`}
            style={{
              width: `${Math.min(100, 100 * displayRatio(part.bytes, memory.totalBytes))}%`,
            }}
          />
        ))}
      </div>
      <ul className="ingot-legend">
        {parts.map((part) => (
          <li key={part.key}>
            <i className={`ingot-key ingot-${part.key}`} aria-hidden="true" />
            {part.word} <strong>{formatBytes(part.bytes)}</strong>
          </li>
        ))}
        {level !== "ok" && (
          <li>
            <StatusPill tone={level === "serious" ? "bad" : "warn"}>{LOW_WORDS[level]}</StatusPill>
          </li>
        )}
      </ul>
    </div>
  );
}
