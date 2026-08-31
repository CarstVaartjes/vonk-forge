import {useEffect, useId, useMemo, useRef, useState} from "react";
import type {KeyboardEvent} from "react";
import type {
  LibraryApi,
  LibraryPlacementApplication,
  LibraryPlacementPreview,
} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {TechnicalDetails} from "./library-technical-details";

export type LibraryPlacementInvocation = "drag-drop" | "keyboard" | "button";

function errorMessage(value: unknown): string {
  return (value instanceof Error ? value.message : "The Controller did not complete the request.").slice(0, 256);
}

function terminal(state: LibraryPlacementApplication["state"]): boolean {
  return ["succeeded", "failed", "cancelled"].includes(state);
}

function stateLabel(state: LibraryPlacementApplication["state"]): string {
  if (state === "waiting-for-operator") return "Waiting for operator";
  return state.charAt(0).toUpperCase() + state.slice(1);
}

function defaultAlias(title: string): string {
  const value = title.toLocaleLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 128);
  return value || "model";
}

function completedSteps(application: LibraryPlacementApplication): number {
  const completed = application.progress.completed_steps;
  return typeof completed === "number" && Number.isFinite(completed)
    ? Math.max(0, Math.min(application.total_steps, completed))
    : Math.max(0, Math.min(application.total_steps, application.current_step));
}

function PlacementPlan({nodeNames, plan}: {nodeNames: Record<string, string>; plan: LibraryPlacementPreview}) {
  return <div className="library-placement-plan">
    <div className="library-placement-outcome">
      <strong>{plan.desired_state === "running" ? "Install and start" : "Install and keep ready"}</strong>
      <span>{plan.selected_node_ids.map(nodeId => nodeNames[nodeId] ?? nodeId).join(" + ")}</span>
      {plan.locations.running && <small>This exact recipe is already running on the selected placement.</small>}
      {!plan.locations.running && plan.locations.installed && <small>This exact recipe is already installed on the selected placement.</small>}
    </div>
    <ol className="library-placement-steps" aria-label="Placement plan steps">
      {plan.steps.map(step => <li key={`${step.index}-${step.kind}`}><span>{step.index + 1}</span><div><strong>{step.label}</strong>{(step.node_ids?.length ?? 0) > 0 && <small>{step.node_ids?.map(nodeId => nodeNames[nodeId] ?? nodeId).join(" + ")}</small>}</div></li>)}
    </ol>
    <div className="library-placement-nodes" role="list" aria-label="Selected Spark capacity">
      {plan.selected_nodes.map(node => <article key={node.node_id} role="listitem">
        <header><strong>{nodeNames[node.node_id] ?? node.node_id}</strong><span>Rank {node.rank} · {node.role}{node.endpoint_owner ? " · endpoint" : ""}</span></header>
        <dl>
          <div><dt>Disk</dt><dd>{node.disk_required_bytes == null ? "Not reported" : `${formatBytes(node.disk_required_bytes)} required`}{node.disk_free_after_bytes == null ? "" : ` · ${formatBytes(node.disk_free_after_bytes)} free after`}</dd></div>
          <div><dt>Memory</dt><dd>{node.memory_required_bytes == null ? "Not reported" : `${formatBytes(node.memory_required_bytes)} required`}{node.memory_free_after_bytes == null ? "" : ` · ${formatBytes(node.memory_free_after_bytes)} free after`}</dd></div>
        </dl>
      </article>)}
    </div>
    {plan.warnings.length > 0 && <section className="library-placement-reasons is-warning" aria-label="Placement warnings"><strong>Review before applying</strong><ul>{plan.warnings.map(reason => <li key={`${reason.code}-${reason.detail}`}><span>{reason.detail}</span>{(reason.node_ids?.length ?? 0) > 0 && <small>{reason.node_ids?.map(nodeId => nodeNames[nodeId] ?? nodeId).join(" + ")}</small>}</li>)}</ul></section>}
    {plan.blockers.length > 0 && <section className="library-placement-reasons is-blocked" role="alert"><strong>Placement cannot start</strong><ul>{plan.blockers.map(reason => <li key={`${reason.code}-${reason.detail}`}><span>{reason.detail}</span>{(reason.node_ids?.length ?? 0) > 0 && <small>{reason.node_ids?.map(nodeId => nodeNames[nodeId] ?? nodeId).join(" + ")}</small>}</li>)}</ul></section>}
    <div className="library-digest-confirmation"><span>Authority is locked to this preview</span><small>If Spark state, capacity, or the recipe revision changes, the Controller rejects this plan and asks for a fresh review.</small><TechnicalDetails items={[{label: "Placement digest", value: plan.plan_digest}, {label: "Recipe revision", value: plan.recipe_revision_id}]}/></div>
  </div>;
}

function PlacementProgress({application, nodeNames}: {application: LibraryPlacementApplication; nodeNames: Record<string, string>}) {
  const completed = completedSteps(application);
  const percent = application.total_steps === 0 ? 100 : Math.round((completed / application.total_steps) * 100);
  return <section className={`library-placement-progress state-${application.state}`} aria-label="Placement progress" aria-live="polite" aria-atomic="true">
    <header><div><strong>{stateLabel(application.state)}</strong><span>{application.selected_node_ids.map(nodeId => nodeNames[nodeId] ?? nodeId).join(" + ")}</span></div><b>{percent}%</b></header>
    {application.total_steps > 0 && <div className="library-placement-progress-track" role="progressbar" aria-label="Placement steps completed" aria-valuemin={0} aria-valuemax={application.total_steps} aria-valuenow={completed}><span style={{width: `${percent}%`}}/></div>}
    <p>{completed} of {application.total_steps} step{application.total_steps === 1 ? "" : "s"} complete{application.current_operation_id ? " · current operation is running" : ""}</p>
    {application.status_reason && <p className="library-placement-status-reason" role={application.state === "failed" ? "alert" : "status"}>{application.status_reason}</p>}
    {(application.locations.installed || application.locations.running) && <p className="library-placement-location-proof"><strong>Location confirmed</strong><span>{application.locations.running ? "Running" : "Installed"} on {application.selected_node_ids.map(nodeId => nodeNames[nodeId] ?? nodeId).join(" + ")}</span></p>}
    <TechnicalDetails items={[{label: "Placement operation", value: application.id}, {label: "Authority digest", value: application.plan_digest}]}/>
  </section>;
}

export function LibraryPlacementDialog({api, invocation, nodeIds, nodeNames, onBusyChange, onClose, onRefresh, recipeId, recipeTitle}: {
  api: LibraryApi;
  invocation: LibraryPlacementInvocation;
  nodeIds: string[];
  nodeNames: Record<string, string>;
  onBusyChange?(busy: boolean): void;
  onClose(): void;
  onRefresh(signal: AbortSignal): Promise<void>;
  recipeId: string;
  recipeTitle: string;
}) {
  const [desiredState, setDesiredState] = useState<"installed" | "running">("installed");
  const [alias, setAlias] = useState(() => defaultAlias(recipeTitle));
  const [plan, setPlan] = useState<LibraryPlacementPreview>();
  const [application, setApplication] = useState<LibraryPlacementApplication>();
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [applyError, setApplyError] = useState("");
  const [progressError, setProgressError] = useState("");
  const [refreshError, setRefreshError] = useState("");
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [progressAttempt, setProgressAttempt] = useState(0);
  const [refreshAttempt, setRefreshAttempt] = useState(0);
  const [stale, setStale] = useState(false);
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const applyController = useRef<AbortController | undefined>(undefined);
  const requestKey = useRef("");
  const mounted = useRef(true);
  const titleId = useId();
  const sortedNodeIds = useMemo(() => [...new Set(nodeIds)].sort(), [nodeIds]);
  const validAlias = /^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$/.test(alias);

  useEffect(() => () => { mounted.current = false; applyController.current?.abort(); }, []);

  useEffect(() => {
    if (application) return;
    if (desiredState === "running" && !validAlias) {
      setPlan(undefined);
      setLoading(false);
      setPreviewError("");
      return;
    }
    const controller = new AbortController();
    requestKey.current = crypto.randomUUID();
    setLoading(true);
    setPreviewError("");
    setApplyError("");
    setStale(false);
    setPlan(undefined);
    void api.previewLibraryPlacement({
      alias: desiredState === "running" ? alias : null,
      desired_state: desiredState,
      invocation,
      node_ids: sortedNodeIds,
      recipe_id: recipeId,
    }, controller.signal)
      .then(value => { if (!controller.signal.aborted) setPlan(value); })
      .catch(value => { if (!controller.signal.aborted) setPreviewError(errorMessage(value)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [alias, api, application, desiredState, invocation, previewAttempt, recipeId, sortedNodeIds]);

  useEffect(() => {
    if (!application || terminal(application.state)) return;
    const controller = new AbortController();
    let timer = 0;
    async function poll() {
      try {
        const next = await api.libraryPlacement(application!.id, controller.signal);
        if (controller.signal.aborted) return;
        setApplication(next);
        setProgressError("");
        if (terminal(next.state)) return;
        timer = window.setTimeout(() => void poll(), 1500);
      } catch (value) {
        if (!controller.signal.aborted) setProgressError(errorMessage(value));
      }
    }
    timer = window.setTimeout(() => void poll(), progressAttempt === 0 ? 700 : 0);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [api, application?.id, application?.state, onRefresh, progressAttempt]);

  useEffect(() => {
    if (!application || !terminal(application.state)) return;
    const controller = new AbortController();
    setRefreshError("");
    void onRefresh(controller.signal).catch(value => {
      if (!controller.signal.aborted) setRefreshError(errorMessage(value));
    });
    return () => controller.abort();
  }, [application?.id, application?.state, onRefresh, refreshAttempt]);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    close.current?.focus();
    return () => { document.body.style.overflow = previousOverflow; };
  }, []);

  useEffect(() => { onBusyChange?.(applying); }, [applying, onBusyChange]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);
  useEffect(() => {
    if (!applying) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [applying]);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      if (!applying) onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])') ?? []);
    if (controls.length === 0) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || !dialog.current?.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !dialog.current?.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
  }

  async function applyPlan() {
    if (!plan?.allowed || applying || stale || !requestKey.current) return;
    const controller = new AbortController();
    applyController.current?.abort();
    applyController.current = controller;
    setApplying(true);
    setApplyError("");
    try {
      const value = await api.applyLibraryPlacement({
        alias: plan.alias,
        desired_state: plan.desired_state,
        invocation: plan.invocation,
        node_ids: plan.selected_node_ids,
        plan_digest: plan.plan_digest,
        recipe_id: plan.recipe_id,
        request_key: requestKey.current,
      }, controller.signal);
      if (mounted.current && !controller.signal.aborted) setApplication(value);
    } catch (value) {
      if (!mounted.current || controller.signal.aborted) return;
      const detail = errorMessage(value);
      setApplyError(detail);
      if (/stale|digest.*(?:changed|mismatch|invalid)/i.test(detail)) setStale(true);
    } finally {
      if (mounted.current && !controller.signal.aborted) setApplying(false);
      if (applyController.current === controller) applyController.current = undefined;
    }
  }

  function reviewFresh() {
    setApplication(undefined);
    setPreviewAttempt(value => value + 1);
  }

  const operationActive = Boolean(application && !terminal(application.state));
  return <div className="library-dialog-backdrop" onMouseDown={event => { if (!applying && event.target === event.currentTarget) onClose(); }}>
    <div className="library-action-dialog library-placement-dialog" ref={dialog} role="dialog" aria-modal="true" aria-labelledby={titleId} onKeyDown={onKeyDown}>
      <header><div><p>Server-authoritative placement</p><h3 id={titleId}>Place {recipeTitle}</h3></div><button ref={close} type="button" className="button secondary" disabled={applying} onClick={onClose}>Close</button></header>
      <div className="library-action-dialog-body">
        {!application && <fieldset className="library-placement-intent" disabled={applying || loading}>
          <legend>Desired result</legend>
          <label><input type="radio" name="desired-placement-state" value="installed" checked={desiredState === "installed"} onChange={() => setDesiredState("installed")}/><span><strong>Install</strong><small>Download and prepare the recipe without starting an endpoint.</small></span></label>
          <label><input type="radio" name="desired-placement-state" value="running" checked={desiredState === "running"} onChange={() => setDesiredState("running")}/><span><strong>Install and run</strong><small>Prepare the recipe and expose a ready endpoint.</small></span></label>
          {desiredState === "running" && <label className="library-placement-alias"><span>Endpoint alias</span><input value={alias} maxLength={128} aria-invalid={!validAlias} aria-describedby="library-placement-alias-help" onChange={event => setAlias(event.target.value.toLocaleLowerCase())}/><small id="library-placement-alias-help">Lowercase letters, numbers, dots, underscores, and hyphens.</small></label>}
        </fieldset>}
        {!application && loading && <p role="status">Checking exact recipe, capacity, and Spark authority…</p>}
        {!application && previewError && <div className="fleet-error" role="alert"><p>{previewError}</p><button type="button" onClick={() => setPreviewAttempt(value => value + 1)}>Retry placement preview</button></div>}
        {!application && plan && <PlacementPlan nodeNames={nodeNames} plan={plan}/>} 
        {applyError && <div className="fleet-error" role="alert"><p>{applyError}</p><p>{stale ? "Spark or recipe authority changed. Review a fresh plan before applying." : "The request may have reached the Controller. Retry uses the same request key and cannot create a duplicate placement."}</p>{stale && <button type="button" onClick={() => setPreviewAttempt(value => value + 1)}>Review fresh plan</button>}</div>}
        {application && <PlacementProgress application={application} nodeNames={nodeNames}/>} 
        {progressError && <div className="fleet-error" role="alert"><p>Progress is temporarily unavailable: {progressError}</p><button type="button" onClick={() => setProgressAttempt(value => value + 1)}>Retry progress</button></div>}
        {refreshError && <div className="fleet-error" role="alert"><p>Placement finished, but Library and Spark state could not be refreshed: {refreshError}</p><button type="button" onClick={() => setRefreshAttempt(value => value + 1)}>Retry Library refresh</button></div>}
      </div>
      <footer>
        {application ? <>
          {(application.state === "failed" || application.state === "cancelled") && <button type="button" className="button secondary" onClick={reviewFresh}>Review recovery plan</button>}
          <button type="button" className="button" onClick={onClose}>{operationActive ? "Continue in background" : "Done"}</button>
        </> : <>
          <button type="button" className="button secondary" disabled={applying} onClick={onClose}>Cancel</button>
          <button type="button" className="button" disabled={!plan?.allowed || applying || stale || (desiredState === "running" && !validAlias)} onClick={() => void applyPlan()}>{applying ? "Starting placement…" : desiredState === "running" ? "Install and run" : "Install on selected Sparks"}</button>
        </>}
      </footer>
    </div>
  </div>;
}
