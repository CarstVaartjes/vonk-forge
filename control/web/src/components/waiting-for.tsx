import {Time} from "./time";

type Blocker = {code: string; detail: string; severity?: string; node_ids?: readonly string[]};

/** What a queued or blocked operation is waiting for, and when it is checked again. */
export function WaitingFor({blockers, nextAttemptAt}: {blockers?: readonly Blocker[] | null; nextAttemptAt?: string | null}) {
  if (!blockers?.length && !nextAttemptAt) return null;
  return <ul className="waiting-for" aria-label="Waiting for">
    {(blockers ?? []).map(item => <li key={`${item.code}:${(item.node_ids ?? []).join(",")}`}><strong>{item.code}</strong> {item.detail}{item.node_ids?.length ? <small> · {item.node_ids.join(", ")}</small> : null}</li>)}
    {nextAttemptAt && <li>Next attempt <Time value={nextAttemptAt}/></li>}
  </ul>;
}
