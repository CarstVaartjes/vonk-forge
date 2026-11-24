import "./activity-preview.css";
import { useEffect, useRef, useState, type MouseEvent } from "react";
import type { ControlApi, VisualFleetNode } from "../../api/types";
import { activityPreviewRows, unsuccessfulCount } from "../../pages/activity-records";
import { nodeDisplayName } from "../../lib/fleet";
import { exactTime, formatDate } from "../../lib/time";
import { StatusPill } from "./status-pill";

const ROWS = 7;
const REFRESH_MS = 30_000;

type Rows = ReturnType<typeof activityPreviewRows>;

/** Today's rows read as a time, older ones as a date. */
function whenText(value: string | null, now: Date): string {
  const at = value ? new Date(value) : null;
  if (!at || Number.isNaN(at.getTime())) return "";
  return at.toDateString() === now.toDateString()
    ? at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false })
    : formatDate(at);
}

/** The latest activity, read from the same operations list the Activity page uses, and read again every 30 seconds. */
export function ActivityPreview({
  api,
  nodes,
  now,
  onViewAll,
  onUnsuccessful: onUnsuccessfulProp,
}: {
  api: Pick<ControlApi, "operations">;
  nodes: VisualFleetNode[];
  now: Date;
  /** How many operations in the page this rail reads ended unsuccessfully, for the page's link to Activity. */
  onUnsuccessful?: ((count: number) => void) | undefined;
  onViewAll?: ((event: MouseEvent<HTMLAnchorElement>) => void) | undefined;
}) {
  // undefined: not read yet; null: the last read failed, which is not the same as nothing happening
  const [rows, setRows] = useState<Rows | null>();
  // Each read names Sparks as they are called now.
  const names = useRef(new Map<string, string>());
  // The read effect must not restart when the parent re-renders.
  const onUnsuccessful = useRef(onUnsuccessfulProp);
  onUnsuccessful.current = onUnsuccessfulProp;
  names.current = new Map(nodes.map((node) => [node.id, nodeDisplayName(node)]));
  useEffect(() => {
    const controller = new AbortController();
    const read = () =>
      api.operations(undefined, controller.signal).then(
        (response) => {
          // Unknown membership is not "nothing happened".
          if (controller.signal.aborted) return;
          setRows(
            response.operations === null
              ? null
              : activityPreviewRows(response.operations, names.current, ROWS),
          );
          onUnsuccessful.current?.(
            response.operations === null
              ? 0
              : unsuccessfulCount(response.operations, names.current),
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
                <strong title={row.label}>{row.label}</strong>
              </span>
              <StatusPill tone={row.tone}>{row.word}</StatusPill>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
