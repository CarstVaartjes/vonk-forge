import "./no-reading.css";
import { NOT_REPORTED_TEXT } from "../../lib/fleet";

/**
 * The one way a figure with no reading is drawn: a muted dash. The words stay for screen readers;
 * `hint` adds a visible helper line under the dash for a headline figure.
 */
export function NoReading({ hint }: { hint?: string }) {
  return (
    <span className="no-reading">
      <span aria-hidden="true">—</span>
      <span className="sr-only">{NOT_REPORTED_TEXT}</span>
      {hint && <small className="no-reading-hint">{hint}</small>}
    </span>
  );
}
