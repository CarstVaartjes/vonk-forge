import {useState} from "react";
import type {ControlApi, ReconcilePlan} from "../api/types";
import {safeErrorText} from "../lib/error-display";
import {ConfirmDialog} from "./confirm-dialog";
import {useToast} from "./toast";

/** Review the exact plan, then reconcile one invalid stopped installation (`vonkctl recipe installation reconcile`). */
export function InstallationReconcile({api, installationId}: {api: Pick<ControlApi, "previewInstallationReconcile" | "reconcileInstallation">; installationId: string}) {
  const [plan, setPlan] = useState<ReconcilePlan>();
  const [busy, setBusy] = useState(false);
  const [requestKey] = useState(() => crypto.randomUUID());
  const toast = useToast();
  async function review() {
    setBusy(true);
    try {
      const preview = await api.previewInstallationReconcile(installationId);
      if (preview.allowed) setPlan(preview);
      else toast.error(`Reconcile is blocked. ${preview.blockers.map(item => item.detail).join(" ")}`.trim());
    } catch (value) { toast.error(safeErrorText(value instanceof Error ? value.message : "Reconcile review failed", 256)); }
    finally { setBusy(false); }
  }
  async function confirm() {
    setBusy(true);
    try { await api.reconcileInstallation(installationId, requestKey); toast.info("Reconcile queued. Follow it in Activity."); }
    catch (value) { toast.error(safeErrorText(value instanceof Error ? value.message : "Reconcile failed", 256)); }
    finally { setBusy(false); setPlan(undefined); }
  }
  return <>
    <button type="button" className="button secondary" disabled={busy} onClick={() => void review()}>Review reconcile</button>
    {plan && <ConfirmDialog title="Reconcile this installation?" consequence={`Reconcile will run ${plan.phases.length} step${plan.phases.length === 1 ? "" : "s"}${plan.reclaimed_bytes ? ` and reclaim ${plan.reclaimed_bytes} bytes` : ""}.`} confirmLabel="Reconcile" command={`vonkctl recipe installation reconcile ${installationId}`} busy={busy} onConfirm={() => void confirm()} onCancel={() => setPlan(undefined)}/>}
  </>;
}
