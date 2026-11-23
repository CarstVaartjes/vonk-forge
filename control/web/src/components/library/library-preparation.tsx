import { compareWire, displayRatio } from "../../api/contract-numeric";
import type { ControlApi, LibraryViewModel } from "../../api/types";
import { formatBytes } from "../../lib/fleet";
import { CancelOperation } from "../ui/cancel-operation";
import { WaitingFor } from "../ui/waiting-for";

type Preparation = NonNullable<LibraryViewModel["local"]["preparation"]>;

/** The one line a live preparation reads as: phase, then how much of it is done. */
export function preparationLabel(preparation: Preparation): string {
  const phase = preparation.phase ?? preparation.state;
  const total = preparation.total_bytes;
  if (total == null || compareWire(total, 0) <= 0) return phase;
  const completed = preparation.completed_bytes ?? 0;
  const percent = Math.min(100, Math.round(displayRatio(completed, total) * 100));
  return `${phase} · ${formatBytes(completed)} of ${formatBytes(total)} · ${percent}%`;
}

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
    <div className="library-preparation">
      <span role="status">{preparationLabel(preparation)}</span>
      <WaitingFor blockers={preparation.blockers} nextAttemptAt={preparation.next_attempt_at} />
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
