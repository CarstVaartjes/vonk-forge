import {useState} from "react";
import {safeErrorText} from "../lib/error-display";
import {ConfirmDialog} from "./confirm-dialog";
import {useToast} from "./toast";

/**
 * Cancel one accepted operation. The Controller owns cleanup and keeps
 * resumable work, so the confirmation states the consequence and the request
 * key is stable across retries of an unclear response.
 */
export function CancelOperation<T>({what, consequence, command, cancel, cancellationEvidence}: {what: string; consequence: string; command?: string; cancel(requestKey: string): Promise<T>; cancellationEvidence?: (result: T) => string}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [requested, setRequested] = useState(false);
  const [requestKey] = useState(() => crypto.randomUUID());
  const toast = useToast();
  async function confirm() {
    setBusy(true);
    try {
      const result = await cancel(requestKey);
      // Accepting the request is not the same as the work having stopped: the
      // owner observes the operation and unmounts this control at its terminal state.
      const state = result && typeof result === "object" ? (result as {state?: unknown}).state : undefined;
      if (state === "cancelled" && cancellationEvidence) toast.info(cancellationEvidence(result));
      else if (state === "cancelled") toast.success(`Cancelled the ${what}.`);
      else { setRequested(true); toast.info(`Cancellation requested for the ${what}. It is cancelling until it stops.`); }
    }
    catch (value) { toast.error(safeErrorText(value instanceof Error ? value.message : "Cancellation failed", 256)); }
    finally { setBusy(false); setConfirming(false); }
  }
  return <>
    <button type="button" className="button secondary" disabled={requested} onClick={() => setConfirming(true)}>{requested ? `Cancelling ${what}…` : `Cancel ${what}`}</button>
    {confirming && <ConfirmDialog title={`Cancel this ${what}?`} consequence={consequence} confirmLabel={`Confirm cancel ${what}`} command={command} busy={busy} onConfirm={() => void confirm()} onCancel={() => setConfirming(false)}/>}
  </>;
}
