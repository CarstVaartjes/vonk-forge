import { nodeHealth } from "../lib/node-health";
import { InstallationReconcile } from "./installation-reconcile";
import { StatusPill } from "./status-pill";
import type { ControlApi } from "../api/types";

/** A Spark's Health card: each finding as a shape and a word, worst first, with what it is about. */
export function HealthList({
  api,
  rows,
}: {
  api: ControlApi;
  rows: ReturnType<typeof nodeHealth>;
}) {
  return (
    <ul className="health-list" aria-label="Health">
      {rows.map((row) => (
        <li key={row.key}>
          <StatusPill tone={row.tone}>{row.title}</StatusPill>
          {row.detail && <span>{row.detail}</span>}
          {row.installationId && (
            <InstallationReconcile api={api} installationId={row.installationId} />
          )}
        </li>
      ))}
      {rows.length === 0 && <li>Nothing to report.</li>}
    </ul>
  );
}
