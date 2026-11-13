import type { useFleetStream } from "../hooks/use-fleet-stream";
import { staleWord } from "../hooks/fleet-stream-state";
import { ObservationNotice } from "./observation-notice";
import { SkeletonRows } from "./skeleton";
import { StatusPill } from "./status-pill";

/** What the fleet pages say while the Fleet loads, fails, or is showing its last known state. */
export function FleetFeedback({ fleet }: { fleet: ReturnType<typeof useFleetStream> }) {
  return (
    <>
      {fleet.snapshot && fleet.stale && (
        <div className="fleet-stale">
          <StatusPill tone="warn">{staleWord(fleet.refreshError)}</StatusPill>
          <ObservationNotice
            connection="reconnecting"
            lastSuccessAt={fleet.lastUpdatedAt}
            subject="the Fleet"
          />
        </div>
      )}
      {fleet.loading && !fleet.snapshot && !fleet.error && (
        <SkeletonRows columns={6} label="Loading Fleet" />
      )}
      {fleet.error && !fleet.snapshot && (
        <section className="fleet-error" role="alert">
          <h2>Fleet unavailable</h2>
          <p>{fleet.error}</p>
          <button type="button" className="button" onClick={fleet.retry}>
            Retry
          </button>
        </section>
      )}
    </>
  );
}
