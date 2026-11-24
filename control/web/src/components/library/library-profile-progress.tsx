import type { ReactNode } from "react";
import type { FleetProfileApplicationView } from "../../api/types";
import { CancelOperation } from "../ui/cancel-operation";
import { WaitingFor } from "../ui/waiting-for";
import { useLibraryNodeName } from "./library-node-names";
import { availabilityProgress, LibraryAvailabilityProgress } from "./library-availability-progress";

function applicationProgressRecord(
  application: FleetProfileApplicationView,
): Record<string, unknown> {
  const progress = application.progress as Record<string, unknown>;
  const child = progress.child_progress;
  return child && typeof child === "object" ? (child as Record<string, unknown>) : progress;
}

export function ProfileProgress({
  application,
  cancel,
  notice,
}: {
  application: FleetProfileApplicationView;
  cancel?: ((requestKey: string) => Promise<unknown>) | undefined;
  notice?: ReactNode;
}) {
  const progress = applicationProgressRecord(application);
  // The child's measured operation carries bytes, rate, ETA and a bar per Spark. The child's
  // own byte counts are the whole-load budget, a different quantity, so they are not shown here.
  const measured = availabilityProgress(progress.operation ?? { phase: progress.phase });
  const nodeName = useLibraryNodeName();
  const nodeIds = Array.isArray(progress.node_ids)
    ? progress.node_ids.filter((id): id is string => typeof id === "string")
    : [];
  return (
    <section
      className={`library-profile-application state-${application.state}`}
      aria-live="polite"
      aria-label="Profile load progress"
    >
      <div className="library-profile-application-heading">
        <span>{application.state.replaceAll("-", " ")}</span>
      </div>
      <LibraryAvailabilityProgress progress={measured} />
      {nodeIds.length > 0 && measured.members.length <= 1 && (
        <ul className="library-profile-application-members" aria-label="Profile load targets">
          {nodeIds.map((nodeId) => (
            <li key={nodeId}>
              <span>{nodeName(nodeId)}</span>
              <small>Participating</small>
            </li>
          ))}
        </ul>
      )}
      {application.status_reason && <p>{application.status_reason}</p>}
      {application.state === "superseded" && (
        <p className="library-profile-application-superseded">
          Not a failure:{" "}
          {application.superseded_by ? (
            <>
              this load continues as application <code>{application.superseded_by}</code>.
            </>
          ) : (
            "a newer profile intent replaced this load; review it again."
          )}
        </p>
      )}
      <WaitingFor
        blockers={application.blockers}
        nextAttemptAt={application.next_attempt_at}
        phase={measured.phase}
      />
      {notice}
      {cancel && (
        <CancelOperation
          what="load"
          consequence="Stops this profile load and reconciles what it already changed. Sparks may be left partly changed until you load again."
          command={`vonkctl profile cancel ${application.id}`}
          cancel={cancel}
        />
      )}
    </section>
  );
}
