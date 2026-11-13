import "./activity-preview.css";
import { useEffect, useRef, useState, type MouseEvent } from "react";
import type { ControlApi, VisualFleetNode } from "../api/types";
import { activityPreviewRows } from "../pages/activity";
import { nodeDisplayName } from "../lib/fleet";
import { exactTime } from "../lib/time";
import { StatusPill } from "./status-pill";

const ROWS = 7;
const REFRESH_MS = 30_000;

type Rows = ReturnType<typeof activityPreviewRows>;

/** Today's rows read as a time, older ones as a short day. */
function whenText(value: string | null, now: Date): string {
  const at = value ? new Date(value) : null;
  if (!at || Number.isNaN(at.getTime())) return "";
  return at.toDateString() === now.toDateString()
    ? at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false })
    : at.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

/** The latest activity, read from the same operations list the Activity page uses, and read again every 30 seconds. */
export function ActivityPreview({
  api,
  nodes,
  now,
  onViewAll,
}: {
  api: Pick<ControlApi, "operations">;
  nodes: VisualFleetNode[];
  now: Date;
  onViewAll?(event: MouseEvent<HTMLAnchorElement>): void;
}) {
  // undefined: not read yet; null: the last read failed, which is not the same as nothing happening
  const [rows, setRows] = useState<Rows | null>();
  // Each read names Sparks as they are called now.
  const names = useRef(new Map<string, string>());
  names.current = new Map(nodes.map((node) => [node.id, nodeDisplayName(node)]));
  useEffect(() => {
    const controller = new AbortController();
    const read = () =>
      api.operations(undefined, controller.signal).then(
        (response) => {
          // Unknown membership is not "nothing happened".
          if (!controller.signal.aborted)
            setRows(
              response.operations === null
                ? null
                : activityPreviewRows(response.operations, names.current, ROWS),
            );
        },
        () => {
          if (!controller.signal.aborted) setRows(null);
        },
      );
    void read();
    const timer = setInterval(() => void read(), REFRESH_MS);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [api]);
  return (
    <section className="card activity-preview" aria-labelledby="activity-preview-heading">
      <header>
        <h2 id="activity-preview-heading">Activity</h2>
        <a href="/activity" onClick={onViewAll}>
          View all
        </a>
      </header>
      {rows === undefined ? null : rows === null ? (
        <p role="status">Activity is not available right now.</p>
      ) : rows.length === 0 ? (
        <p>No activity yet.</p>
      ) : (
        <ul>
          {rows.map((row) => (
            <li key={row.id}>
              <time title={(row.occurredAt && exactTime(row.occurredAt)) || undefined}>
                {whenText(row.occurredAt, now)}
              </time>
              <span className="activity-preview-what">
                <strong>{row.label}</strong>
                {row.targets && <small>{row.targets}</small>}
              </span>
              <StatusPill tone={row.tone}>{row.word}</StatusPill>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
