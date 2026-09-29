import {useState} from "react";

/**
 * Cancel one accepted operation. The Controller owns cleanup and keeps
 * resumable work, so the confirmation states the consequence and the request
 * key is stable across retries of an unclear response.
 */
export function CancelOperation({what, consequence, cancel}: {what: string; consequence: string; cancel(requestKey: string): Promise<unknown>}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [requestKey] = useState(() => crypto.randomUUID());
  async function confirm() {
    setBusy(true); setError("");
    try { await cancel(requestKey); setConfirming(false); }
    catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Cancellation failed"); }
    finally { setBusy(false); }
  }
  if (!confirming) return <button type="button" className="button secondary" onClick={() => setConfirming(true)}>Cancel {what}</button>;
  return <span className="library-cancel-operation" role="group" aria-label={`Confirm cancel ${what}`}>
    <span>{consequence}</span>
    <button type="button" className="button danger" disabled={busy} onClick={() => void confirm()}>{busy ? "Cancelling…" : `Confirm cancel ${what}`}</button>
    <button type="button" className="button secondary" disabled={busy} onClick={() => setConfirming(false)}>Keep running</button>
    {error && <span role="alert">{error}</span>}
  </span>;
}
