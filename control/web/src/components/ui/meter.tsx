import type { Vital } from "../../lib/fleet";
import { NoReading } from "./no-reading";
/** A gauge with its words beside it, so colour is never the only signal. */
export function Meter({ label, vital }: { label: string; vital: Vital }) {
  if (vital.text === null) return <NoReading hint={vital.reason} />;
  if (vital.percent === null) return <>{vital.text}</>;
  return (
    <span className="meter-cell">
      {/* biome-ignore lint/a11y/useSemanticElements: a styled bar; a native meter element cannot take this styling */}
      <span
        className={vital.amber ? "meter meter-high" : "meter"}
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={vital.percent}
        aria-valuetext={vital.text}
      >
        <span style={{ width: `${vital.percent}%` }} />
      </span>
      <span>{vital.text}</span>
    </span>
  );
}
