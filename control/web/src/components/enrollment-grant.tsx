import {useEffect, useState} from "react";
import type {ControlApi, EnrollmentGrantResponse, EnrollmentGrantStatus} from "../api/types";
import {safeErrorText} from "../lib/error-display";
import {ConfirmPanel} from "./confirm-panel";
import {CopyButton} from "./copy-button";

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

export function enrollmentCommand(grant: EnrollmentGrantResponse): string {
  return `curl -fsSL ${grant.installer_url} | ${grant.controller_address ? `VONK_CONTROLLER_ADDRESS=${shellQuote(grant.controller_address)} ` : ""}VONK_ENROLLMENT_URL=${shellQuote(grant.enrollment_endpoint)} VONK_CONTROLLER_CA_SHA256=${shellQuote(grant.ca_fingerprint)} sh -s -- --enroll`;
}

/** One-use grant: install command, pairing token (shown once), live status, revoke. */
export function EnrollmentGrant({api, grant}: {api: ControlApi; grant: EnrollmentGrantResponse}) {
  const [status, setStatus] = useState<EnrollmentGrantStatus | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const command = enrollmentCommand(grant);
  const open = !status || status.state === "pending";

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const poll = () => api.enrollmentStatus(grant.id, controller.signal).then(setStatus, () => undefined);
    void poll();
    const timer = setInterval(() => void poll(), 5000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [api, grant.id, open]);

  async function revoke() {
    setBusy(true);
    try {
      setStatus(await api.revokeEnrollment(grant.id));
      setConfirming(false);
    } catch (failure) {
      setError(safeErrorText(failure instanceof Error ? failure.message : "Could not revoke the grant"));
    } finally {
      setBusy(false);
    }
  }

  return <div className="fleet-enrollment-result">
    <p><strong>Grant expires:</strong> <time dateTime={grant.expires_at}>{new Date(grant.expires_at).toLocaleString()}</time></p>
    <p>Run this command on the Spark. The installer will ask for the one-use pairing token.</p>
    <pre><code>{command}</code></pre><CopyButton label="command" value={command}/>
    <p><label htmlFor={`grant-token-${grant.id}`}>One-use pairing token</label></p>
    <input id={`grant-token-${grant.id}`} readOnly value={grant.token} aria-label="One-use pairing token"/><CopyButton label="pairing token" value={grant.token}/>
    <p role="status" data-grant-state={status?.state ?? "unknown"}><strong>Grant status:</strong> {status?.state ?? "checking…"}{status?.state === "consumed" && status.display_name ? ` by ${status.display_name}` : ""}</p>
    {open && !confirming && <button type="button" className="button secondary" onClick={() => setConfirming(true)}>Revoke grant</button>}
    {confirming && <ConfirmPanel title="Revoke this grant?" consequence="The pairing token stops working. A Spark that has not enrolled yet cannot use it." confirmLabel="Revoke grant" command={`vonkctl fleet enrollment revoke ${grant.id} --yes`} busy={busy} onConfirm={() => void revoke()} onCancel={() => setConfirming(false)}/>}
    {error && <p role="alert">{error}</p>}
  </div>;
}

