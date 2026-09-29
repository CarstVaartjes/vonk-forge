import {useState} from "react";
import {safeErrorText} from "../lib/error-display";
import {ConfirmDialog} from "./confirm-dialog";
import {useToast} from "./toast";

/**
 * Cancel one accepted operation. The Controller owns cleanup and keeps
 * resumable work, so the confirmation states the consequence and the request
 * key is stable across retries of an unclear response.
 */
export function CancelOperation({what, consequence, command, cancel}: {what: string; consequence: string; command?: string; cancel(requestKey: string): Promise<unknown>}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [requestKey] = useState(() => crypto.randomUUID());
  const toast = useToast();
  async function confirm() {
    setBusy(true);
    try { await cancel(requestKey); toast.success(`Cancelled the ${what}.`); }
    catch (value) { toast.error(safeErrorText(value instanceof Error ? value.message : "Cancellation failed", 256)); }
    finally { setBusy(false); setConfirming(false); }
  }
  return <>
    <button type="button" className="button secondary" onClick={() => setConfirming(true)}>Cancel {what}</button>
    {confirming && <ConfirmDialog title={`Cancel this ${what}?`} consequence={consequence} confirmLabel={`Confirm cancel ${what}`} command={command} busy={busy} onConfirm={() => void confirm()} onCancel={() => setConfirming(false)}/>}
  </>;
}
