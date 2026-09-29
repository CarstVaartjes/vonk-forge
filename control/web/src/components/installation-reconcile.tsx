import {useState} from "react";
import type {ControlApi, ReconcilePlan} from "../api/types";

/** Review the exact plan, then reconcile one invalid stopped installation (`vonkctl recipe installation reconcile`). */
export function InstallationReconcile({api, installationId}: {api: Pick<ControlApi, "previewInstallationReconcile" | "reconcileInstallation">; installationId: string}) {
  const [plan, setPlan] = useState<ReconcilePlan>();
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState("");
  const [requestKey] = useState(() => crypto.randomUUID());
  async function run(action: () => Promise<void>) {
    setBusy(true); setError("");
    try { await action(); } catch (value) { setError(value instanceof Error ? value.message.slice(0, 256) : "Reconcile failed"); }
    finally { setBusy(false); }
  }
  if (done) return <p role="status">Reconcile started. Follow it in Activity.</p>;
  return <span className="library-cancel-operation">
    {!plan && <button type="button" className="button secondary" disabled={busy} onClick={() => void run(async () => setPlan(await api.previewInstallationReconcile(installationId)))}>Review reconcile</button>}
    {plan && <>
      <span>{plan.allowed ? `Reconcile will run ${plan.phases.length} step${plan.phases.length === 1 ? "" : "s"}${plan.reclaimed_bytes ? ` and reclaim ${plan.reclaimed_bytes} bytes` : ""}.` : "Reconcile is blocked."}</span>
      {plan.blockers.length > 0 && <ul>{plan.blockers.map(item => <li key={item.code}>{item.detail}</li>)}</ul>}
      <button type="button" className="button danger" disabled={busy || !plan.allowed} onClick={() => void run(async () => { await api.reconcileInstallation(installationId, requestKey); setDone(true); })}>Confirm reconcile</button>
      <button type="button" className="button secondary" disabled={busy} onClick={() => setPlan(undefined)}>Cancel</button>
    </>}
    {error && <span role="alert">{error}</span>}
  </span>;
}
