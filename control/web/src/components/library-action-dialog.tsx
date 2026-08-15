import {useEffect, useId, useRef, useState} from "react";
import type {KeyboardEvent} from "react";
import type {
  LibraryApi,
  LibraryInstallPlan,
  LibraryLoadPlan,
  LibraryMappingPlan,
  LibraryOperation,
  LibraryStopPlan,
  LibraryUninstallPlan,
} from "../api/types";
import {
  InstallPreview,
  LoadPreview,
  MappingPreview,
  StopPreview,
  UninstallPreview,
} from "./library-action-preview";
import type {LibraryActionPlan} from "./library-action-preview";
import {actionName} from "./library-action-types";
import type {LibraryActionName, LibraryActionTarget} from "./library-action-types";

function message(value: unknown): string {
  return (value instanceof Error ? value.message : "The control authority request failed.").slice(0, 256);
}

function preview(api: LibraryApi, target: LibraryActionTarget): Promise<LibraryActionPlan> {
  if (target.kind === "mapping") return api.previewLibraryMapping(target.input);
  if (target.kind === "install") return api.previewLibraryInstall(target.input);
  if (target.kind === "run") return api.previewLibraryLoad(target.input);
  if (target.kind === "stop") return api.previewLibraryStop(target.runId);
  return api.previewLibraryUninstall(target.installationId);
}

function allowed(plan: LibraryActionPlan): boolean {
  return !("allowed" in plan) || plan.allowed;
}

function applyLabel(target: LibraryActionTarget): string {
  if (target.kind === "mapping") return "Create selected mapping";
  if (target.kind === "install") return "Install on selected nodes";
  if (target.kind === "run") return "Load selected installation";
  if (target.kind === "stop") return "Stop selected run";
  return "Remove selected installation";
}

function Plan({plan, target}: {plan: LibraryActionPlan; target: LibraryActionTarget}) {
  if (target.kind === "mapping") return <MappingPreview plan={plan as LibraryMappingPlan}/>;
  if (target.kind === "install") return <InstallPreview plan={plan as LibraryInstallPlan}/>;
  if (target.kind === "run") return <LoadPreview plan={plan as LibraryLoadPlan}/>;
  if (target.kind === "stop") return <StopPreview plan={plan as LibraryStopPlan}/>;
  return <UninstallPreview plan={plan as LibraryUninstallPlan}/>;
}

async function apply(api: LibraryApi, target: LibraryActionTarget, plan: LibraryActionPlan, alias: string): Promise<LibraryOperation | null> {
  if (target.kind === "mapping") {
    const mapping = plan as LibraryMappingPlan;
    await api.applyLibraryMapping({...target.input, placement_digest: mapping.placement_digest});
    return null;
  }
  if (target.kind === "install") {
    const install = plan as LibraryInstallPlan;
    return api.applyLibraryInstall({...target.input, plan_digest: install.plan_digest});
  }
  if (target.kind === "run") {
    const load = plan as LibraryLoadPlan;
    return api.applyLibraryLoad({...target.input, alias, plan_digest: load.plan_digest});
  }
  if (target.kind === "stop") return api.applyLibraryStop(target.runId, (plan as LibraryStopPlan).plan_digest);
  return api.applyLibraryUninstall(target.installationId, (plan as LibraryUninstallPlan).plan_digest);
}

export function LibraryActionDialog({alias, api, onApplied, onClose, onRefresh, target}: {
  alias: string;
  api: LibraryApi;
  onApplied(operation: LibraryOperation, name: LibraryActionName): void;
  onClose(): void;
  onRefresh(): Promise<void>;
  target: LibraryActionTarget;
}) {
  const [plan, setPlan] = useState<LibraryActionPlan>();
  const [previewError, setPreviewError] = useState("");
  const [applyError, setApplyError] = useState("");
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [stale, setStale] = useState(false);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const dialog = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const name = actionName(target);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setPreviewError("");
    setApplyError("");
    setStale(false);
    setPlan(undefined);
    void preview(api, target)
      .then(value => { if (active) setPlan(value); })
      .catch(value => { if (active) setPreviewError(message(value)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [api, previewAttempt, target]);

  useEffect(() => {
    close.current?.focus();
  }, []);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])') ?? []);
    if (controls.length === 0) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  async function applyPlan() {
    if (!plan || !allowed(plan)) return;
    setApplying(true);
    setApplyError("");
    try {
      const operation = await apply(api, target, plan, alias);
      if (operation) onApplied(operation, name);
      setApplying(false);
      onClose();
      void onRefresh();
    } catch (value) {
      const detail = message(value);
      setApplyError(detail);
      if (/stale|digest.*(?:changed|mismatch|invalid)/i.test(detail)) setStale(true);
      setApplying(false);
    }
  }

  return <div className="library-dialog-backdrop">
    <div className="library-action-dialog" ref={dialog} role="dialog" aria-modal="true" aria-labelledby={titleId} onKeyDown={onKeyDown}>
      <header><div><p className="fleet-kicker">Server authority preview</p><h3 id={titleId}>Review {name}</h3></div><button ref={close} type="button" className="icon-button" onClick={onClose} aria-label="Close review">×</button></header>
      <div className="library-action-dialog-body">
        {loading && <p role="status">Loading {name} preview…</p>}
        {previewError && <div className="fleet-error" role="alert"><p>{previewError}</p><button type="button" onClick={() => setPreviewAttempt(value => value + 1)}>Retry preview</button></div>}
        {plan && <Plan plan={plan} target={target}/>}
        {applyError && <div className="fleet-error" role="alert"><p>{applyError}</p><p>The reviewed authority remains open. Review again if the underlying state changed.</p>{stale && <button type="button" onClick={() => setPreviewAttempt(value => value + 1)}>Review fresh preview</button>}</div>}
      </div>
      <footer>
        <button type="button" className="button secondary" onClick={onClose}>Cancel</button>
        <button type="button" className="button" disabled={!plan || !allowed(plan) || applying || stale} onClick={() => void applyPlan()}>{applying ? "Applying…" : applyLabel(target)}</button>
      </footer>
    </div>
  </div>;
}
