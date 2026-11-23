import type { ControlApi, LibraryViewModel } from "../../api/types";
import { CancelOperation } from "../ui/cancel-operation";
import { availabilityProgress, LibraryAvailabilityProgress } from "./library-availability-progress";
import { WaitingFor } from "../ui/waiting-for";

type Preparation = NonNullable<LibraryViewModel["local"]["preparation"]>;

/**
 * A preparation the Controller reports as live: its progress, what it waits for
 * and Cancel, all read from the library state rather than from this page's
 * memory, so a reload or another tab shows the same thing. A recipe's
 * preparation is a model-cache operation too, so both cancel through it.
 */
export function LibraryPreparation({
  api,
  preparation,
  what,
}: {
  api: ControlApi;
  preparation: Preparation;
  what: string;
}) {
  const operationId = preparation.operation_id;
  return (
    <div className="library-preparation" role="status">
      <LibraryAvailabilityProgress
        progress={availabilityProgress({
          ...preparation,
          phase: preparation.phase ?? preparation.state,
        })}
      />
      <WaitingFor
        blockers={preparation.blockers}
        nextAttemptAt={preparation.next_attempt_at}
        phase={preparation.phase ?? undefined}
      />
      {operationId && (
        <CancelOperation
          what={what}
          consequence="Stops this download. Partial files are kept and the download resumes if you start it again."
          command={`vonkctl model cancel ${operationId} --yes`}
          cancel={(key) => api.cancelModelOperation(operationId, key)}
          cancellationEvidence={(result) =>
            result.cancellation?.observation?.detail ??
            "Cancellation ended; stopping the writer remains unconfirmed."
          }
        />
      )}
    </div>
  );
}
