import { type WireNumber, formatWire } from "../api/contract-numeric";

// NVML nvmlClocksEventReason* bits (nvml.h), in bit order. `throttling` is false for the reasons that
// are the clock working as designed (idle, an applications-clocks setting, sync boost, a display setting).
const REASONS = [
  { bit: 0x1n, word: "GPU idle", throttling: false },
  { bit: 0x2n, word: "application clocks setting", throttling: false },
  { bit: 0x4n, word: "software power cap", throttling: true },
  { bit: 0x8n, word: "hardware slowdown", throttling: true },
  { bit: 0x10n, word: "sync boost", throttling: false },
  { bit: 0x20n, word: "software thermal slowdown", throttling: true },
  { bit: 0x40n, word: "hardware thermal slowdown", throttling: true },
  { bit: 0x80n, word: "hardware power brake slowdown", throttling: true },
  { bit: 0x100n, word: "display clock setting", throttling: false },
] as const;
const KNOWN = REASONS.reduce((all, reason) => all | reason.bit, 0n);

/**
 * The driver's clock event (throttle) bitmask in words, shared with `vonkctl` through
 * `tests/fixtures/fleet-wording-throttle.json`: zero says none active, known bits in bit order, and any bit
 * outside the table adds "state unknown". `mood` says whether anything is actually throttling the clock.
 */
export function throttleReasons(reasons: WireNumber): {
  words: string;
  mood: "clear" | "throttled" | "unrecognised";
} {
  const mask = BigInt(formatWire(reasons));
  const present = REASONS.filter((reason) => mask & reason.bit);
  const extra = (mask & ~KNOWN) !== 0n;
  const words = [...present.map((reason) => reason.word), ...(extra ? ["state unknown"] : [])];
  return {
    words: words.length ? words.join(", ") : "none active",
    mood: present.some((reason) => reason.throttling)
      ? "throttled"
      : extra
        ? "unrecognised"
        : "clear",
  };
}
