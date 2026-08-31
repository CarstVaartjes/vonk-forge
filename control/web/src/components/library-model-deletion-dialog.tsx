import {useEffect, useId, useRef, useState} from "react";
import type {KeyboardEvent} from "react";
import type {LibraryApi, LibraryModelDeletionPlan, LibraryOperation} from "../api/types";
import {ApiError} from "../api/client";
import {formatBytes} from "../lib/fleet";
import {LibraryOperationProgress, operationSettled} from "./library-operation-progress";
import {LibraryPlanReasons} from "./library-plan-reasons";
import {TechnicalDetails} from "./library-technical-details";

export type {LibraryModelDeletionPlan} from "../api/types";

function errorMessage(value: unknown): string {
  return (value instanceof Error ? value.message : "The Controller could not complete this request.").slice(0, 256);
}

export function LibraryModelDeletionDialog({api, modelTitle, modelVersionSha256, nodeNames, onBusyChange, onClose, onRefresh}: {
  api: LibraryApi;
  modelTitle: string;
  modelVersionSha256: string;
  nodeNames: Record<string, string>;
  onBusyChange?(busy: boolean): void;
  onClose(): void;
  onRefresh(signal: AbortSignal): Promise<void>;
}) {
  const [plan, setPlan] = useState<LibraryModelDeletionPlan>();
  const [previewError, setPreviewError] = useState("");
  const [applyError, setApplyError] = useState("");
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [stale, setStale] = useState(false);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [operation, setOperation] = useState<LibraryOperation>();
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const applyController = useRef<AbortController | undefined>(undefined);
  const requestKey = useRef("");
  const mounted = useRef(false);
  const titleId = useId();

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      applyController.current?.abort();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    requestKey.current = crypto.randomUUID();
    setPlan(undefined);
    setPreviewError("");
    setApplyError("");
    setConfirmed(false);
    setStale(false);
    setLoading(true);
    void api.previewLibraryModelDeletion(modelVersionSha256, controller.signal)
      .then(value => { if (!controller.signal.aborted) setPlan(value); })
      .catch(value => { if (!controller.signal.aborted) setPreviewError(errorMessage(value)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, modelVersionSha256, previewAttempt]);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    close.current?.focus();
    return () => { document.body.style.overflow = previousOverflow; };
  }, []);

  useEffect(() => { onBusyChange?.(applying || Boolean(operation && !operationSettled(operation.state))); }, [applying, onBusyChange, operation]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);

  useEffect(() => {
    if (!applying && (!operation || operationSettled(operation.state))) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [applying, operation]);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const busy = applying || Boolean(operation && !operationSettled(operation.state));
    if (event.key === "Escape") {
      event.preventDefault();
      if (!busy) onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? []);
    if (controls.length === 0) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || !dialog.current?.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !dialog.current?.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
  }

  async function applyPlan() {
    if (!plan?.allowed || !confirmed || applying || !requestKey.current) return;
    const controller = new AbortController();
    applyController.current?.abort();
    applyController.current = controller;
    setApplying(true);
    setApplyError("");
    try {
      const next = await api.deleteLibraryModel(modelVersionSha256, {plan_digest: plan.plan_digest, request_key: requestKey.current}, controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      setOperation(next);
      if (operationSettled(next.state)) await onRefresh(controller.signal);
    } catch (value) {
      if (!mounted.current || controller.signal.aborted) return;
      const detail = errorMessage(value);
      setApplyError(detail);
      setStale((value instanceof ApiError && value.status === 409) || /stale|digest.*(?:changed|mismatch|invalid)/i.test(detail));
    } finally {
      if (mounted.current && !controller.signal.aborted) setApplying(false);
      if (applyController.current === controller) applyController.current = undefined;
    }
  }

  const busy = applying || Boolean(operation && !operationSettled(operation.state));
  return <div className="library-dialog-backdrop" onMouseDown={event => { if (!busy && event.target === event.currentTarget) onClose(); }}>
    <div className="library-action-dialog library-model-deletion-dialog" ref={dialog} role="dialog" aria-modal="true" aria-labelledby={titleId} onKeyDown={onKeyDown}>
      <header><div><p className="fleet-kicker">Fleet-wide dependency preview</p><h3 id={titleId}>Delete {modelTitle} from Sparks</h3></div><button ref={close} type="button" className="icon-button" disabled={busy} onClick={onClose} aria-label="Close model deletion review">×</button></header>
      <div className="library-action-dialog-body">
        {loading && <p role="status">Checking every installed recipe and Spark…</p>}
        {previewError && <div className="fleet-error" role="alert"><p>{previewError}</p><button type="button" onClick={() => setPreviewAttempt(value => value + 1)}>Retry preview</button></div>}
        {plan && <div className="action-preview library-model-deletion-preview">
          <p><strong>{plan.installations.length} recipe installation{plan.installations.length === 1 ? "" : "s"} across {plan.nodes.length} Spark{plan.nodes.length === 1 ? "" : "s"} will be removed.</strong></p>
          <p>{formatBytes(plan.bytes_removed)} of this exact model and its dependent recipe installations will be removed.</p>
          <section aria-label="Affected Sparks"><h4>Affected Sparks</h4><ol className="action-node-plans">{plan.nodes.map(node => <li key={node.node_id}><strong>{nodeNames[node.node_id] ?? node.node_id}</strong><span>{node.recipe_ids.length} recipe{node.recipe_ids.length === 1 ? "" : "s"} · {formatBytes(node.installed_bytes)}</span><TechnicalDetails compact items={[{label: "Node ID", value: node.node_id}, ...node.recipe_ids.map((recipeId, index) => ({label: `Recipe ${index + 1} ID`, value: recipeId}))]}/></li>)}</ol></section>
          <section aria-label="Affected recipe installations"><h4>Dependent recipes removed</h4><ol className="action-node-plans">{plan.installations.map((installation, index) => <li key={installation.installation_id}><strong>Installation {index + 1}</strong><span>{installation.node_ids.map(nodeId => nodeNames[nodeId] ?? nodeId).join(" + ")} · {formatBytes(installation.installed_bytes)}</span><TechnicalDetails compact items={[{label: "Installation ID", value: installation.installation_id}, {label: "Recipe ID", value: installation.recipe_id}, {label: "Recipe revision ID", value: installation.recipe_revision_id}]}/></li>)}</ol></section>
          {plan.active_runs.length > 0 && <section aria-label="Active runs"><h4>{plan.active_run_count} active run{plan.active_run_count === 1 ? " blocks" : "s block"} deletion</h4><p>Forge never stops runs automatically. Stop every complete run, then request a fresh deletion preview.</p><ul>{plan.active_runs.map(run => <li key={run.run_id}>{run.alias} · {run.state} · route {run.route_state}<TechnicalDetails compact items={[{label: "Run ID", value: run.run_id}]}/></li>)}</ul></section>}
          <p className="authority-copy">Shared cache policy: {plan.shared_cache_policy}</p>
          <LibraryPlanReasons heading="Delete blockers" reasons={plan.blockers}/>
          <LibraryPlanReasons heading="Delete warnings" reasons={plan.warnings}/>
          <div className="library-digest-confirmation"><span>Authority is locked to this fleet-wide preview</span><small>Any changed installation, run, or dependency invalidates this plan and requires a fresh review.</small><TechnicalDetails items={[{label: "Model digest", value: plan.model_version_sha256}, {label: "Plan digest", value: plan.plan_digest}]}/></div>
          {plan.allowed && !operation && <label className="library-destructive-confirmation"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)}/><span>I understand that every listed recipe installation and this exact model will be removed from the listed Sparks.</span></label>}
        </div>}
        {applyError && <div className="fleet-error" role="alert"><p>{applyError}</p>{stale ? <button type="button" onClick={() => setPreviewAttempt(value => value + 1)}>Review fresh preview</button> : <button type="button" onClick={() => void applyPlan()}>Retry deletion request</button>}</div>}
        {operation && <LibraryOperationProgress api={api} name="Delete model" onChange={setOperation} onRefresh={onRefresh} operation={operation}/>}
      </div>
      <footer>
        <button type="button" className="button secondary" disabled={busy} onClick={onClose}>{operation && operationSettled(operation.state) ? "Close" : "Cancel"}</button>
        {!operation && <button type="button" className="button danger" disabled={!plan?.allowed || !confirmed || applying || stale} onClick={() => void applyPlan()}>{applying ? "Starting deletion…" : "Delete model and dependent recipes"}</button>}
      </footer>
    </div>
  </div>;
}
