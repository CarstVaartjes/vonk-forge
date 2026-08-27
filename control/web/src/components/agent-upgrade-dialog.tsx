import {useEffect, useRef, useState} from "react";
import type {KeyboardEvent as ReactKeyboardEvent} from "react";
import type {AgentUpgradePlan, AgentUpgradeStrategy, ControlApi} from "../api/types";

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
  const dialog = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);

  useEffect(() => { closeButton.current?.focus(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setPlan(undefined);
    setError("");
    void api.previewAgentUpgrade(node ? [node.id] : undefined, strategy, controller.signal)
      .then(setPlan)
      .catch(value => {
        if (!controller.signal.aborted) setError(value instanceof Error ? value.message : "The upgrade preview is unavailable.");
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [api, node, strategy]);
  useEffect(() => {
    onBusyChange?.(applying);
    return () => onBusyChange?.(false);
  }, [applying, onBusyChange]);

  function handleKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape" && !applying) { event.preventDefault(); onClose(); return; }
    if (event.key !== "Tab") return;
    const focusable = [...(dialog.current?.querySelectorAll<HTMLElement>("a[href],button:not([disabled]),select:not([disabled])") ?? [])];
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

  const title = node ? `Upgrade ${node.name}` : "Upgrade Spark agents";
  return <div className="library-dialog-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !applying) onClose(); }}>
    <div ref={dialog} className="library-action-dialog agent-upgrade-dialog" role="dialog" aria-modal="true" aria-labelledby="agent-upgrade-title" aria-busy={loading || applying || undefined} onKeyDown={handleKeyDown}>
      <header><div><p className="fleet-kicker">Signed agent rollout</p><h3 id="agent-upgrade-title">{title}</h3><p className="dialog-subtitle">No SSH is used. Each Spark installs the exact signed release through its narrow package helper.</p></div><button ref={closeButton} type="button" className="icon-button" disabled={applying} onClick={onClose} aria-label={`Close ${title}`}>×</button></header>
      <div className="library-action-dialog-body">
        {!node && <label className="agent-upgrade-strategy"><span>Rollout</span><select aria-label="Rollout strategy" value={strategy} disabled={applying} onChange={event => setStrategy(event.currentTarget.value as AgentUpgradeStrategy)}><option value="one-at-a-time">One at a time</option><option value="all-at-once">All at once</option></select><small>{strategy === "one-at-a-time" ? "The controller proves each new runtime identity before choosing the next Spark." : "The controller dispatches every eligible Spark immediately and verifies each reconnect independently."}</small></label>}
        {loading && <p role="status">Checking the current signed release and eligible Sparks…</p>}
        {error && <p className="dialog-error" role="alert">{error}</p>}
        {plan && !jobId && <section className="agent-upgrade-preview" aria-label="Agent upgrade preview"><dl><div><dt>Release</dt><dd><strong>{plan.package.package_version}</strong></dd></div><div><dt>Targets</dt><dd>{plan.node_ids.length} {plan.node_ids.length === 1 ? "Spark" : "Sparks"}</dd></div><div><dt>Rollout</dt><dd>{plan.strategy === "one-at-a-time" ? "One at a time" : "All at once"}</dd></div><div><dt>Package</dt><dd>{(plan.package.package_bytes / 1024 / 1024).toFixed(1)} MiB · <code>{plan.package.package_sha256.slice(0, 12)}…</code></dd></div></dl><p>The controller will preserve enrollment and configuration, restart the agent, and require the exact target build and binary digests before recording success.</p></section>}
        {jobId && <section className="agent-upgrade-success" role="status"><strong>Upgrade queued</strong><p>The controller owns the rollout from here. Progress and any per-Spark failure are recorded in Activity.</p><code>{jobId}</code></section>}
      </div>
      <footer>{jobId ? <><a className="button secondary" href="/activity">View Activity</a><button type="button" className="button" onClick={onClose}>Done</button></> : <><button type="button" className="button secondary" disabled={applying} onClick={onClose}>Cancel</button><button type="button" className="button" disabled={!plan || loading || applying} onClick={() => void apply()}>{applying ? "Queuing…" : node ? "Upgrade this Spark" : "Start rollout"}</button></>}</footer>
    </div>
  </div>;
}
