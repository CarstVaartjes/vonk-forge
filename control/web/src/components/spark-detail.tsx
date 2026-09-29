import {useEffect, useState} from "react";
import type {ControlApi, EnrollmentGrantResponse, FleetLogResponse, VisualFleetNode} from "../api/types";
import {safeErrorText} from "../lib/error-display";
import {nodeCpuClock, nodeDisplayName, nodeRecipeUpdates, nodeStatus} from "../lib/fleet";
import {ConfirmDialog} from "./confirm-dialog";
import {EnrollmentGrant} from "./enrollment-grant";
import {InstallationReconcile} from "./installation-reconcile";
import {SkeletonBlock} from "./skeleton";
import {StatusPill} from "./status-pill";
import {Time} from "./time";
import {useToast} from "./toast";

type Action = "remove" | "re-enroll" | null;
type Tab = "overview" | "settings" | "logs";
const TABS: Array<[Tab, string]> = [["overview", "Overview"], ["settings", "Settings"], ["logs", "Logs"]];

function failure(error: unknown, fallback: string): string {
  return safeErrorText(error instanceof Error ? error.message : fallback);
}

export function SparkDetail({api, id, onClose}: {api: ControlApi; id: string; onClose(): void}) {
  const [node, setNode] = useState<VisualFleetNode | null>(null);
  const [logs, setLogs] = useState<FleetLogResponse | null>(null);
  const [grant, setGrant] = useState<EnrollmentGrantResponse | null>(null);
  const [action, setAction] = useState<Action>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  useEffect(() => {
    const controller = new AbortController();
    setNode(null); setLogs(null); setGrant(null); setAction(null); setTab("overview"); setError(null);
    api.fleetNode(id, controller.signal).then(loaded => { setNode(loaded); setName(loaded.display_name); }, cause => { if (!controller.signal.aborted) setError(failure(cause, "Could not read this Spark")); });
    return () => controller.abort();
  }, [api, id]);

  async function run(work: () => Promise<string>, fallback: string) {
    setBusy(true);
    try { toast.success(await work()); } catch (cause) { toast.error(failure(cause, fallback)); } finally { setBusy(false); setAction(null); }
  }
  const rename = () => run(async () => {
    const identity = await api.renameFleetNode(id, name.trim());
    setNode(current => current && {...current, display_name: identity.display_name});
    return `Spark renamed to ${identity.display_name}.`;
  }, "Could not rename this Spark");
  const remove = () => run(async () => { await api.removeFleetNode(id); onClose(); return "Spark removed."; }, "Could not remove this Spark");
  const reenroll = () => run(async () => {
    const response = await api.reenrollFleetNode(id, crypto.randomUUID());
    if (!response.grant) throw new Error("Controller did not return an enrollment grant");
    setGrant(response.grant);
    return "Re-enrollment grant created.";
  }, "Could not create a re-enrollment grant");
  async function loadLogs() {
    setBusy(true); setError(null);
    try { setLogs(await api.fleetLogs(id)); } catch (cause) { setError(failure(cause, "Could not read recent logs")); } finally { setBusy(false); }
  }
  function openTab(next: Tab) {
    setTab(next);
    if (next === "logs" && !logs && !busy) void loadLogs();
  }

  const title = node ? nodeDisplayName(node) : id;
  const cpu = node ? nodeCpuClock(node) : null;
  const status = node ? nodeStatus(node, new Date()) : {status: "offline" as const, reasons: []};
  return <section className="spark-detail" aria-labelledby="spark-detail-heading">
    <header><h2 id="spark-detail-heading">{title}</h2><button type="button" className="button secondary" onClick={onClose}>Close</button></header>
    {!node && !error && <SkeletonBlock label="Reading Spark"/>}
    {node && <>
      <div role="tablist" aria-label="Spark sections" className="button-row">{TABS.map(([key, label]) => <button key={key} type="button" role="tab" id={`spark-tab-${key}`} aria-selected={tab === key} aria-controls="spark-tab-panel" className={tab === key ? "button" : "button secondary"} onClick={() => openTab(key)}>{label}</button>)}</div>
      <div role="tabpanel" id="spark-tab-panel" aria-labelledby={`spark-tab-${tab}`}>
      {tab === "overview" && <>
        <dl className="spark-facts">
          <div><dt>Status</dt><dd><StatusPill tone={status.status === "online" ? "healthy" : status.status === "offline" ? "danger" : "warning"}>{status.status}</StatusPill></dd></div>
          <div><dt>Last seen</dt><dd><Time value={node.connection.last_seen_at}/></dd></div>
          <div><dt>Host</dt><dd>{node.hostname}{node.ip_address ? ` · ${node.ip_address}` : ""}</dd></div>
          {cpu && <div><dt>CPU clock</dt><dd>{cpu.clock}{cpu.temperature ? ` · ${cpu.temperature}` : ""}{cpu.lowClock && <> <StatusPill tone="warning">low under load</StatusPill></>}</dd></div>}
          <div><dt>Recipes</dt><dd>{node.loaded.filter(item => item.healthy).length} running · {node.installed.filter(item => item.complete).length} installed</dd></div>
        </dl>
        {node.loaded.some(item => Object.keys(item.option_choices ?? {}).length > 0) && <ul className="spark-run-options" aria-label="Active recipe options">{node.loaded.filter(item => Object.keys(item.option_choices ?? {}).length > 0).map(item => <li key={`${item.run_id}:${item.rank}`}><strong>{item.title}</strong> {Object.entries(item.option_choices ?? {}).map(([name, value]) => <StatusPill key={name} tone="neutral">{name}: {value}</StatusPill>)}</li>)}</ul>}
        {status.reasons.length > 0 && <ul className="spark-warnings" aria-label="Needs attention">{status.reasons.map((reason, index) => <li key={index}><StatusPill tone="warning">{status.status === "offline" ? "offline" : "attention"}</StatusPill> {reason}</li>)}</ul>}
        {nodeRecipeUpdates(node).length > 0 && <ul className="spark-warnings" aria-label="Updates available">{nodeRecipeUpdates(node).map(update => <li key={update.runId}><StatusPill tone="info">update available</StatusPill> {update.detail}</li>)}</ul>}
        {node.installed.some(item => !item.complete) && <ul className="spark-warnings" aria-label="Incomplete installations">{node.installed.filter(item => !item.complete).map(item => <li key={item.installation_id}><StatusPill tone="warning">{item.group_state}</StatusPill> {item.title} · {item.present_ranks.length} of {item.expected_rank_count} ranks <InstallationReconcile api={api} installationId={item.installation_id}/></li>)}</ul>}
      </>}
      {tab === "settings" && <>
        <form className="settings-form" onSubmit={event => { event.preventDefault(); if (name.trim()) void rename(); }}>
          <label>Spark name<input required maxLength={80} value={name} onChange={event => setName(event.currentTarget.value)}/></label>
          <div className="button-row"><button type="submit" className="button" disabled={busy || !name.trim() || name.trim() === node.display_name}>Save name</button></div>
          <p>CLI: <code>vonkctl fleet rename {id} &lt;name&gt;</code></p>
        </form>
        <section className="danger-zone" aria-labelledby="spark-danger-heading">
          <h3 id="spark-danger-heading">Danger zone</h3>
          <div className="button-row">
            <button type="button" className="button secondary" onClick={() => setAction("re-enroll")}>Re-enroll</button>
            <button type="button" className="button danger" onClick={() => setAction("remove")}>Remove</button>
          </div>
          {action === "re-enroll" && <ConfirmDialog title="Re-enroll this Spark?" consequence="Its current agent credentials are replaced. The Spark keeps its name and recipes but must run the new installer command to reconnect." confirmLabel="Create re-enrollment grant" command={`vonkctl fleet re-enroll ${id} --yes`} busy={busy} onConfirm={() => void reenroll()} onCancel={() => setAction(null)}/>}
          {action === "remove" && <ConfirmDialog title="Remove this Spark?" consequence="It leaves the fleet and its agent credentials are revoked. Enrolling it again needs a new grant. This cannot be undone." confirmLabel="Remove Spark" typeName={node.display_name} command={`vonkctl fleet remove ${id} --yes`} busy={busy} onConfirm={() => void remove()} onCancel={() => setAction(null)}/>}
          {grant && <EnrollmentGrant api={api} grant={grant}/>}
        </section>
      </>}
      {tab === "logs" && <div className="spark-logs">{!logs && !error && <SkeletonBlock lines={3} label="Reading recent logs"/>}{logs && (logs.entries.length === 0 ? <p>No log lines retained for this Spark yet. Try again after it reports activity.</p> : <ol>{logs.entries.map(entry => <li key={entry.evidence_id}><Time value={entry.observed_at}/> <StatusPill tone={entry.level === "error" ? "danger" : entry.level === "warning" ? "warning" : "neutral"}>{entry.level}</StatusPill> <small>{entry.source}</small> {entry.message}</li>)}</ol>)}<p>CLI: <code>vonkctl fleet loginfo {id}</code></p></div>}
      </div>
    </>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
