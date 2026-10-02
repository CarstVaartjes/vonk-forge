import {useEffect, useState} from "react";
import {ageLabel} from "../hooks/use-operation-observer";

/**
 * The shared notice for observed remote work: a lost connection keeps the last
 * known state on screen with its age, and a long wait says the work continues.
 */
export function ObservationNotice({connection, lastSuccessAt, background, subject = "this operation"}: {
  connection: "live" | "reconnecting";
  lastSuccessAt?: number;
  background?: boolean;
  subject?: string;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (connection === "live") return;
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [connection]);
  if (connection === "reconnecting") return <p className="observation-notice" role="status">Reconnecting to the Controller. Showing the last known state of {subject}, updated {ageLabel(lastSuccessAt, now)}. It keeps running either way.</p>;
  if (background) return <p className="observation-notice" role="status">Still running in the background. This page keeps checking; you can leave and come back.</p>;
  return null;
}
