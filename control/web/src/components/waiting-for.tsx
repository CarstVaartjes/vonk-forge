import type { OperationBlocker } from "../api/types";
import { swapShortfallText } from "../lib/fleet";
import { Time } from "./time";

type Blocker = Pick<OperationBlocker, "code" | "detail"> &
  Partial<Pick<OperationBlocker, "severity" | "node_ids" | "swap">>;

/** What a queued or blocked operation is waiting for, and when it is checked again. */
export function WaitingFor({
  blockers,
  nextAttemptAt,
}: {
  blockers?: readonly Blocker[] | null;
  nextAttemptAt?: string | null;
}) {
  if (!blockers?.length && !nextAttemptAt) return null;
  return (
    <ul className="waiting-for" aria-label="Waiting for">
      {(blockers ?? []).map((item) => (
        <li key={`${item.code}:${(item.node_ids ?? []).join(",")}`}>
          <strong>{item.code}</strong> {item.detail}
          {item.node_ids?.length ? <small> · {item.node_ids.join(", ")}</small> : null}
          {item.swap ? (
            <small className="waiting-for-swap"> {swapShortfallText(item.swap)}</small>
          ) : null}
        </li>
      ))}
      {nextAttemptAt && (
        <li>
          Next attempt <Time value={nextAttemptAt} />
        </li>
      )}
    </ul>
  );
}
