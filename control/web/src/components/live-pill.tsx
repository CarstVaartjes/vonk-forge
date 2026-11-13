import type { LiveStatus } from "../hooks/fleet-stream-state";
import { Time } from "./time";

const WORDS = { live: "Live", delayed: "Delayed", reconnecting: "Reconnecting" } as const;

/**
 * The header's stream state: the status vocabulary's shape plus word (the live-pill-* classes share
 * the status-pill shapes), with the last update when known.
 */
export function LivePill({ status }: { status: LiveStatus }) {
  return (
    <span className="live-pill">
      <span className={`status-pill live-pill-${status.state}`}>
        <span className="status-mark" aria-hidden="true" />
        {WORDS[status.state]}
      </span>
      {status.updatedAt !== undefined && (
        <span className="live-pill-age">
          updated <Time value={new Date(status.updatedAt).toISOString()} />
        </span>
      )}
    </span>
  );
}
