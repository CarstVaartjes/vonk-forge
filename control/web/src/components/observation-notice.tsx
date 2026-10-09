import { useEffect, useState } from "react";
import { ageLabel } from "../hooks/use-operation-observer";

/**
 * The shared notice for observed remote work: a lost connection keeps the last
 * known state on screen with its age, and a long wait says the work continues.
 */
export function ObservationNotice({
  connection,
  lastSuccessAt,
  background,
  onResume,
  subject = "this operation",
}: {
  connection: "live" | "reconnecting";
  lastSuccessAt?: number;
  background?: boolean;
  onResume?(): void;
  subject?: string;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (connection === "live" && !background) return;
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [connection, background]);
  if (background)
    return (
      <p className="observation-notice" role="status">
        Observation ended. Showing the last known state of {subject}, updated{" "}
        {ageLabel(lastSuccessAt, now)}.{" "}
        {onResume && (
          <button type="button" onClick={onResume}>
            Check status
          </button>
        )}
      </p>
    );
  if (connection === "reconnecting")
    return (
      <p className="observation-notice" role="status">
        Reconnecting to the Controller. Showing the last known state of {subject}, updated{" "}
        {ageLabel(lastSuccessAt, now)}. Observation does not cancel remote work.
      </p>
    );
  return null;
}
