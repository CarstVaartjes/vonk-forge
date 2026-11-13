import { useEffect, useState } from "react";
import type { ControlApi, FleetLogResponse } from "../api/types";
import { ProfileReasonSeverity } from "../api/vocabulary.generated";
import { errorText } from "../lib/error-display";
import { SkeletonBlock } from "./skeleton";
import { StatusPill } from "./status-pill";
import { Time } from "./time";

/** The Logs tab: the Spark's recent log lines, read when the tab opens. */
export function SparkLogs({ api, id }: { api: ControlApi; id: string }) {
  const [logs, setLogs] = useState<FleetLogResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    api.fleetLogs(id, controller.signal).then(
      (read) => setLogs(read),
      (cause) => {
        if (!controller.signal.aborted) setError(errorText(cause, "Could not read recent logs"));
      },
    );
    return () => controller.abort();
  }, [api, id]);
  return (
    <div className="spark-logs">
      {!logs && !error && <SkeletonBlock lines={3} label="Reading recent logs" />}
      {logs &&
        (logs.entries.length === 0 ? (
          <p>No log lines retained for this Spark yet. Try again after it reports activity.</p>
        ) : (
          <ol>
            {logs.entries.map((entry) => (
              <li key={entry.evidence_id}>
                <Time value={entry.observed_at} />{" "}
                <StatusPill
                  tone={
                    entry.level === ProfileReasonSeverity.ERROR
                      ? "bad"
                      : entry.level === ProfileReasonSeverity.WARNING
                        ? "warn"
                        : "neutral"
                  }
                >
                  {entry.level}
                </StatusPill>{" "}
                <small>{entry.source}</small> {entry.message}
              </li>
            ))}
          </ol>
        ))}
      {error && <p role="alert">{error}</p>}
      <p>
        CLI: <code>vonkctl fleet loginfo {id}</code>
      </p>
    </div>
  );
}
