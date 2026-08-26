import {useEffect, useId, useRef, useState} from "react";
import type {KeyboardEvent} from "react";
import type {
  LibraryApi,
  LibraryBuildPlan,
  LibraryImageDistributionPlan,
  LibraryInstallPlan,
  LibraryLoadPlan,
  LibraryMappingPlan,
  LibraryOperation,
  LibraryStopPlan,
  LibraryUninstallPlan,
  LibrarySnapshot,
} from "../api/types";
import {
  BuildPreview,
  ImageDistributionPreview,
  InstallPreview,
  LoadPreview,
  MappingPreview,
  StopPreview,
  UninstallPreview,
} from "./library-action-preview";
import type {LibraryActionPlan} from "./library-action-preview";
import {actionName} from "./library-action-types";
import type {LibraryActionName, LibraryActionTarget} from "./library-action-types";
import type {LibraryPlacementGroup} from "./library-action-types";
import {TechnicalDetails} from "./library-technical-details";

function message(value: unknown): string {
  return (value instanceof Error ? value.message : "The control authority request failed.").slice(0, 256);
}

function preview(api: LibraryApi, target: LibraryActionTarget, alias: string, signal: AbortSignal): Promise<LibraryActionPlan> {
  if (target.kind === "build") return api.previewLibraryBuild(target.input, signal);
  if (target.kind === "mapping") return api.previewLibraryMapping(target.input, signal);
  if (target.kind === "image_distribution") return api.previewLibraryImageDistribution(target.input, signal);
  if (target.kind === "install") return api.previewLibraryInstall(target.input, signal);
  if (target.kind === "run") return api.previewLibraryLoad({...target.input, alias}, signal);
  if (target.kind === "stop") return api.previewLibraryStop(target.runId, signal);
  return api.previewLibraryUninstall(target.installationId, signal);
}

function allowed(plan: LibraryActionPlan): boolean {
  return !("allowed" in plan) || plan.allowed;
}

function applyLabel(target: LibraryActionTarget): string {
  if (target.kind === "build") return "Build recipe image";
  if (target.kind === "mapping") return "Create selected mapping";
  if (target.kind === "image_distribution") return "Distribute image to selected nodes";
  if (target.kind === "install") return "Install on selected nodes";
  if (target.kind === "run") return "Load selected installation";
  if (target.kind === "stop") return "Stop selected run";
  return "Remove selected installation";
}

function Plan({evidence, plan, policy, previewReceivedAt, target}: {
  evidence?: LibraryPlacementGroup;
  plan: LibraryActionPlan;
  policy: LibrarySnapshot["freshness_policy"];
  previewReceivedAt: number;
  target: LibraryActionTarget;
}) {
  if (target.kind === "build") return <BuildPreview plan={plan as LibraryBuildPlan}/>;
  if (target.kind === "mapping") return <MappingPreview evidence={evidence} plan={plan as LibraryMappingPlan} policy={policy}/>;
  if (target.kind === "image_distribution") return <ImageDistributionPreview plan={plan as LibraryImageDistributionPlan}/>;
  if (target.kind === "install") return <InstallPreview plan={plan as LibraryInstallPlan} policy={policy} previewReceivedAt={previewReceivedAt}/>;
  if (target.kind === "run") return <LoadPreview plan={plan as LibraryLoadPlan}/>;
  if (target.kind === "stop") return <StopPreview plan={plan as LibraryStopPlan}/>;
  return <UninstallPreview plan={plan as LibraryUninstallPlan}/>;
}

async function apply(api: LibraryApi, target: LibraryActionTarget, plan: LibraryActionPlan, requestKey: string, signal: AbortSignal): Promise<LibraryOperation | null> {
  if (target.kind === "build") {
    const build = plan as LibraryBuildPlan;
    return api.applyLibraryBuild({...target.input, build_input_sha256: build.build_input_sha256, request_key: requestKey}, signal);
  }
  if (target.kind === "mapping") {
    const mapping = plan as LibraryMappingPlan;
    await api.applyLibraryMapping({...target.input, placement_digest: mapping.placement_digest, request_key: requestKey}, signal);
    return null;
  }
  if (target.kind === "image_distribution") {
    const distribution = plan as LibraryImageDistributionPlan;
    return api.applyLibraryImageDistribution({...target.input, plan_digest: distribution.plan_digest, request_key: requestKey}, signal);
  }
  if (target.kind === "install") {
    const install = plan as LibraryInstallPlan;
    return api.applyLibraryInstall({...target.input, plan_digest: install.plan_digest, request_key: requestKey}, signal);
  }
  if (target.kind === "run") {
    const load = plan as LibraryLoadPlan;
    return api.applyLibraryLoad({...target.input, alias: load.alias, plan_digest: load.plan_digest, request_key: requestKey}, signal);
  }
  if (target.kind === "stop") return api.applyLibraryStop(target.runId, {plan_digest: (plan as LibraryStopPlan).plan_digest, request_key: requestKey}, signal);
  return api.applyLibraryUninstall(target.installationId, {plan_digest: (plan as LibraryUninstallPlan).plan_digest, request_key: requestKey}, signal);
}

export function LibraryActionDialog({alias, api, evidence, onApplied, onBusyChange, onClose, onRefresh, policy, target}: {
  alias: string;
  api: LibraryApi;
  evidence?: LibraryPlacementGroup;
  onApplied(operation: LibraryOperation, name: LibraryActionName): void;
  onBusyChange?(busy: boolean): void;
  onClose(): void;
  onRefresh(signal: AbortSignal): Promise<void>;
  policy: LibrarySnapshot["freshness_policy"];
  target: LibraryActionTarget;
}) {
  const [plan, setPlan] = useState<LibraryActionPlan>();
  const [previewError, setPreviewError] = useState("");
  const [applyError, setApplyError] = useState("");
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [stale, setStale] = useState(false);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [previewReceivedAt, setPreviewReceivedAt] = useState(0);
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const applyController = useRef<AbortController | undefined>(undefined);
  const mounted = useRef(false);
  const requestKey = useRef("");
  const titleId = useId();
  const name = actionName(target);

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
    setLoading(true);
    setPreviewError("");
    setApplyError("");
    setStale(false);
    setPlan(undefined);
    setPreviewReceivedAt(0);
    void preview(api, target, alias, controller.signal)
      .then(value => {
        if (!controller.signal.aborted) {
          setPreviewReceivedAt(Date.now());
          setPlan(value);
        }
      })
      .catch(value => { if (!controller.signal.aborted) setPreviewError(message(value)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [alias, api, previewAttempt, target]);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    close.current?.focus();
    return () => { document.body.style.overflow = previousOverflow; };
  }, []);

  useEffect(() => {
    if (!applying) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [applying]);

  useEffect(() => { onBusyChange?.(applying); }, [applying, onBusyChange]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      if (!applying) onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? []);
    if (controls.length === 0) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || !dialog.current?.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !dialog.current?.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
  }


  async function applyPlan() {
    if (!plan || !allowed(plan) || applying || !requestKey.current) return;
    const controller = new AbortController();
    applyController.current?.abort();
    applyController.current = controller;
    setApplying(true);
    setApplyError("");
    try {
      const operation = await apply(api, target, plan, requestKey.current, controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      await onRefresh(controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      if (operation) onApplied(operation, name);
      setApplying(false);
      onClose();
    } catch (value) {
      if (!mounted.current || controller.signal.aborted) return;
      const detail = message(value);
      setApplyError(detail);
      if (/stale|digest.*(?:changed|mismatch|invalid)/i.test(detail)) setStale(true);
      setApplying(false);
    } finally {
      if (applyController.current === controller) applyController.current = undefined;
    }
  }

  const digest = plan && "plan_digest" in plan
    ? plan.plan_digest
    : plan && "placement_digest" in plan
      ? plan.placement_digest
      : plan && "build_input_sha256" in plan
        ? plan.build_input_sha256
        : undefined;
  return <div className="library-dialog-backdrop" onMouseDown={event => { if (!applying && event.target === event.currentTarget) onClose(); }}>
    <div className="library-action-dialog" ref={dialog} role="dialog" aria-modal="true" aria-labelledby={titleId} onKeyDown={onKeyDown}>
      <header><div><p className="fleet-kicker">Server authority preview</p><h3 id={titleId}>Review {name}</h3></div><button ref={close} type="button" className="icon-button" disabled={applying} onClick={onClose} aria-label="Close review">×</button></header>
      <div className="library-action-dialog-body">
        {loading && <p role="status">Loading {name} preview…</p>}
        {previewError && <div className="fleet-error" role="alert"><p>{previewError}</p><button type="button" onClick={() => setPreviewAttempt(value => value + 1)}>Retry preview</button></div>}
        {plan && <>
          <Plan evidence={evidence} plan={plan} policy={policy} previewReceivedAt={previewReceivedAt} target={target}/>
          {digest && <div className="library-digest-confirmation"><span>Authority is locked to this preview</span><small>Applying uses the reviewed digest; if authority changes, the action is rejected and must be previewed again.</small><TechnicalDetails items={[{label: "Authority digest", value: digest}]}/></div>}
        </>}
        {applyError && <div className="fleet-error" role="alert"><p>{applyError}</p><p>The reviewed authority remains open. Review again if the underlying state changed.</p>{stale && <button type="button" onClick={() => setPreviewAttempt(value => value + 1)}>Review fresh preview</button>}</div>}
      </div>
      <footer>
        <button type="button" className="button secondary" disabled={applying} onClick={onClose}>Cancel</button>
        <button type="button" className="button" disabled={!plan || !allowed(plan) || applying || stale} onClick={() => void applyPlan()}>{applying ? "Applying…" : applyLabel(target)}</button>
      </footer>
    </div>
  </div>;
}
