import {useEffect, useMemo, useRef, useState} from "react";
import type {KeyboardEvent as ReactKeyboardEvent} from "react";
import type {AgentUpgradePlan, AgentUpgradeStrategy, ControlApi} from "../api/types";
import {parseAgentRepairManifest} from "./agent-repair-manifest";

export function AgentUpgradeDialog({api, node, onBusyChange, onClose}: {
  api: ControlApi;
  node?: {id: string; name: string};
  onBusyChange?(busy: boolean): void;
  onClose(): void;
}) {
  const [strategy, setStrategy] = useState<AgentUpgradeStrategy>("one-at-a-time");
  const [plan, setPlan] = useState<AgentUpgradePlan>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [jobId, setJobId] = useState<string>();
  const [repairMode, setRepairMode] = useState(false);
  const [manifestText, setManifestText] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [previewFailed, setPreviewFailed] = useState(false);
  const [retryNonce, setRetryNonce] = useState(0);
  const [fileError, setFileError] = useState("");
  const dialog = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const parsedRepair = useMemo(
    () => node && repairMode ? parseAgentRepairManifest(manifestText, node.id) : undefined,
    [manifestText, node, repairMode],
  );

  useEffect(() => { closeButton.current?.focus(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    setPlan(undefined);
    setConfirmation("");
    setError("");
    setPreviewFailed(false);
    if (repairMode && (!parsedRepair || !parsedRepair.ok)) {
      setLoading(false);
      if (manifestText.trim() && parsedRepair && !parsedRepair.ok) setError(parsedRepair.error);
      return () => controller.abort();
    }
    setLoading(true);
    void api.previewAgentUpgrade(
      node ? [node.id] : undefined,
      strategy,
      parsedRepair?.ok ? parsedRepair.manifest : undefined,
      controller.signal,
    )
      .then(setPlan)
      .catch(value => {
        if (!controller.signal.aborted) {
          setError(value instanceof Error ? value.message : "The upgrade preview is unavailable.");
          setPreviewFailed(true);
        }
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, manifestText, node, parsedRepair, repairMode, retryNonce, strategy]);
  useEffect(() => {
    onBusyChange?.(applying);
    return () => onBusyChange?.(false);
  }, [applying, onBusyChange]);

  function handleKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape" && !applying) { event.preventDefault(); onClose(); return; }
    if (event.key !== "Tab") return;
    const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>("a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary") ?? [])];
    const first = focusable[0];
    const last = focusable.at(-1);
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  async function apply() {
    if (!plan || applying) return;
    setApplying(true);
    setError("");
    try {
      const result = await api.applyAgentUpgrade(plan);
      setJobId(result.id);
    } catch (value) {
      setError(value instanceof Error ? value.message : "The upgrade could not be queued.");
    } finally {
      setApplying(false);
    }
  }

  async function loadManifest(file: File | undefined) {
    if (!file) return;
    setManifestText("");
    setConfirmation("");
    setFileError("");
    if (file.size > 256 * 1024) {
      setFileError("The repair manifest is larger than 256 KiB.");
      return;
    }
    try {
      setManifestText(await file.text());
    } catch {
      setFileError("The repair manifest file could not be read. Choose it again or paste the JSON.");
    }
  }

  const title = node ? `Upgrade ${node.name}` : "Upgrade Spark agents";
  const repairConfirmation = node ? `Repair ${node.name}` : "";
  const isRepair = Boolean(plan?.repair_manifest);
  return <div className="library-dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !applying) onClose(); }}>
    <div ref={dialog} className="library-action-dialog agent-upgrade-dialog" role="dialog" aria-modal="true" aria-labelledby="agent-upgrade-title" aria-busy={loading || applying || undefined} onKeyDown={handleKeyDown}>
      <header><div><p className="fleet-kicker">Signed agent rollout</p><h3 id="agent-upgrade-title">{title}</h3><p className="dialog-subtitle">No SSH is used. Each Spark installs the exact signed release through its narrow package helper.</p></div><button ref={closeButton} type="button" className="icon-button" disabled={applying} onClick={onClose} aria-label={`Close ${title}`}>×</button></header>
      <div className="library-action-dialog-body">
        {!node && <label className="agent-upgrade-strategy"><span>Rollout</span><select aria-label="Rollout strategy" value={strategy} disabled={applying} onChange={event => setStrategy(event.currentTarget.value as AgentUpgradeStrategy)}><option value="one-at-a-time">One at a time</option><option value="all-at-once">All at once</option></select><small>{strategy === "one-at-a-time" ? "The controller proves each new runtime identity before choosing the next Spark." : "The controller dispatches every eligible Spark immediately and verifies each reconnect independently."}</small></label>}
        {node && <details className="agent-repair-advanced"><summary>Advanced: signed repair capsule</summary><div><label className="agent-repair-toggle"><input type="checkbox" checked={repairMode} disabled={applying} onChange={event => { setRepairMode(event.currentTarget.checked); setManifestText(""); setConfirmation(""); setFileError(""); }}/><span>Use a node-bound repair manifest</span></label>{repairMode && <><p>This path is only for a published recovery capsule bound to this exact Spark. It always runs one at a time.</p><p><strong>Selected Spark ID</strong><br/><code>{node.id}</code></p><label><span>Repair manifest JSON</span><textarea aria-label="Repair manifest JSON" rows={9} spellCheck={false} value={manifestText} onChange={event => { setFileError(""); setManifestText(event.currentTarget.value); }} placeholder='{"schema_version":1,"kind":"agent-upgrade-repair",…}'/></label><label className="agent-repair-file"><span>Or choose a JSON file</span><input type="file" accept="application/json,.json" aria-describedby={fileError ? "agent-repair-file-error" : undefined} onChange={event => { const file = event.currentTarget.files?.[0]; void loadManifest(file); event.currentTarget.value = ""; }}/></label>{fileError && <small id="agent-repair-file-error" className="dialog-error" role="alert">{fileError}</small>}</>}</div></details>}
        {loading && <p role="status">Checking the current signed release and eligible Sparks…</p>}
        {error && <div className="agent-upgrade-error"><p className="dialog-error" role="alert">{error}</p>{previewFailed && <button type="button" className="button secondary" disabled={loading || applying} onClick={() => setRetryNonce(value => value + 1)}>Try preview again</button>}</div>}
        {plan && !jobId && <section className="agent-upgrade-preview" aria-label={isRepair ? "Agent repair preview" : "Agent upgrade preview"}>{isRepair && plan.repair_manifest ? <><dl className="agent-repair-evidence"><div><dt>Spark ID</dt><dd><code>{plan.repair_manifest.node_id}</code></dd></div><div><dt>Authority SHA-256</dt><dd><code>{plan.repair_manifest.authority_sha256}</code></dd></div><div><dt>Release</dt><dd><strong>{plan.package.package_version}</strong></dd></div><div><dt>Package bytes</dt><dd>{plan.package.package_bytes.toLocaleString()}</dd></div><div className="agent-repair-wide"><dt>Immutable package URL</dt><dd><code>{plan.package.package_url}</code></dd></div><div className="agent-repair-wide"><dt>Package SHA-256</dt><dd><code>{plan.package.package_sha256}</code></dd></div><div className="agent-repair-wide"><dt>Package signature</dt><dd><code>{plan.package.package_signature}</code></dd></div><div className="agent-repair-wide"><dt>Target agent SHA-256</dt><dd><code>{plan.package.target_binary_digest}</code></dd></div><div className="agent-repair-wide"><dt>Target build digest</dt><dd><code>{plan.package.target_build_digest}</code></dd></div></dl><p>The controller validates the strict node-bound manifest and exact package descriptor. The Spark package helper verifies the package signature before installation.</p><label className="agent-repair-confirm"><span>Type <strong>{repairConfirmation}</strong> to authorize this one-Spark repair</span><input aria-label="Repair confirmation" autoComplete="off" maxLength={repairConfirmation.length} value={confirmation} onChange={event => setConfirmation(event.currentTarget.value)}/></label></> : <><dl><div><dt>Release</dt><dd><strong>{plan.package.package_version}</strong></dd></div><div><dt>Targets</dt><dd>{plan.node_ids.length} {plan.node_ids.length === 1 ? "Spark" : "Sparks"}</dd></div><div><dt>Rollout</dt><dd>{plan.strategy === "one-at-a-time" ? "One at a time" : "All at once"}</dd></div><div><dt>Package</dt><dd>{(plan.package.package_bytes / 1024 / 1024).toFixed(1)} MiB · <code>{plan.package.package_sha256.slice(0, 12)}…</code></dd></div></dl><p>The controller will preserve enrollment and configuration, restart the agent, and require the exact target build and binary digests before recording success.</p></>}</section>}
        {jobId && <section className="agent-upgrade-success" role="status"><strong>{isRepair ? "Repair queued" : "Upgrade queued"}</strong><p>The controller owns the rollout from here. Progress and any per-Spark failure are recorded in Activity.</p><code>{jobId}</code></section>}
      </div>
      <footer>{jobId ? <><a className="button secondary" href="/activity">View Activity</a><button type="button" className="button" onClick={onClose}>Done</button></> : <><button type="button" className="button secondary" disabled={applying} onClick={onClose}>Cancel</button><button type="button" className="button" disabled={!plan || loading || applying || (isRepair && confirmation !== repairConfirmation)} onClick={() => void apply()}>{applying ? "Queuing…" : isRepair ? repairConfirmation : node ? "Upgrade this Spark" : "Start rollout"}</button></>}</footer>
    </div>
  </div>;
}
