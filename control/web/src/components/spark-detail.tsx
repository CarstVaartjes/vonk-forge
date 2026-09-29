import {useEffect, useState} from "react";
import type {ControlApi, EnrollmentGrantResponse, FleetLogResponse, VisualFleetNode} from "../api/types";
import {safeErrorText} from "../lib/error-display";
import {nodeDisplayName} from "../lib/fleet";
import {ConfirmPanel} from "./confirm-panel";
import {EnrollmentGrant} from "./enrollment-grant";
import {StatusPill} from "./status-pill";
import {Time} from "./time";

type Action = "rename" | "remove" | "re-enroll" | null;

function failure(error: unknown, fallback: string): string {
  return safeErrorText(error instanceof Error ? error.message : fallback);
}

export function SparkDetail({api, id, onClose}: {api: ControlApi; id: string; onClose(): void}) {
  const [node, setNode] = useState<VisualFleetNode | null>(null);
  const [logs, setLogs] = useState<FleetLogResponse | null>(null);
  const [grant, setGrant] = useState<EnrollmentGrantResponse | null>(null);
  const [action, setAction] = useState<Action>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setNode(null); setLogs(null); setGrant(null); setAction(null); setError(null);
    api.fleetNode(id, controller.signal).then(setNode, cause => { if (!controller.signal.aborted) setError(failure(cause, "Could not read this Spark")); });
    return () => controller.abort();
  }, [api, id]);

  async function run(work: () => Promise<void>, fallback: string) {
    setBusy(true); setError(null);
    try { await work(); } catch (cause) { setError(failure(cause, fallback)); } finally { setBusy(false); }
  }
  const rename = () => run(async () => {
    const identity = await api.renameFleetNode(id, name.trim());
    setNode(current => current && {...current, display_name: identity.display_name});
    setAction(null);
  }, "Could not rename this Spark");
  const remove = () => run(async () => { await api.removeFleetNode(id); onClose(); }, "Could not remove this Spark");
  const reenroll = () => run(async () => {
    const response = await api.reenrollFleetNode(id, crypto.randomUUID());
    if (!response.grant) throw new Error("Controller did not return an enrollment grant");
    setGrant(response.grant); setAction(null);
  }, "Could not create a re-enrollment grant");
  const loadLogs = () => run(async () => setLogs(await api.fleetLogs(id)), "Could not read recent logs");

  const title = node ? nodeDisplayName(node) : id;
  return <section className="spark-detail" aria-labelledby="spark-detail-heading">
    <header><h2 id="spark-detail-heading">{title}</h2><button type="button" className="button secondary" onClick={onClose}>Close</button></header>
    {!node && !error && <p role="status">Reading Spark…</p>}
    {node && <>
      <dl className="spark-facts">
        <div><dt>Connection</dt><dd><StatusPill tone={node.connection.online_state === "online" ? "healthy" : "danger"}>{node.connection.online_state}</StatusPill></dd></div>
        <div><dt>Last seen</dt><dd><Time value={node.connection.last_seen_at}/></dd></div>
        <div><dt>Host</dt><dd>{node.hostname}{node.ip_address ? ` · ${node.ip_address}` : ""}</dd></div>
        <div><dt>Recipes</dt><dd>{node.loaded.filter(item => item.healthy).length} running · {node.installed.filter(item => item.complete).length} installed</dd></div>
      </dl>
      {node.warnings.length > 0 && <ul className="spark-warnings" aria-label="Needs attention">{node.warnings.map((warning, index) => <li key={index}><StatusPill tone="warning">{warning.severity}</StatusPill> {warning.detail}</li>)}</ul>}
      <div className="button-row">
        <button type="button" className="button secondary" onClick={() => { setName(node.display_name); setAction("rename"); }}>Rename</button>
        <button type="button" className="button secondary" onClick={() => setAction("re-enroll")}>Re-enroll</button>
        <button type="button" className="button secondary" disabled={busy} onClick={() => void loadLogs()}>Recent logs</button>
        <button type="button" className="button secondary" onClick={() => setAction("remove")}>Remove</button>
      </div>
      {action === "rename" && <form className="confirm-panel" onSubmit={event => { event.preventDefault(); if (name.trim()) void rename(); }}>
        <label>Spark name<input autoFocus required maxLength={80} value={name} onChange={event => setName(event.currentTarget.value)}/></label>
        <div className="button-row"><button type="submit" className="button" disabled={busy}>Save name</button><button type="button" className="button secondary" onClick={() => setAction(null)}>Cancel</button></div>
        <p>CLI: <code>vonkctl fleet rename {id} &lt;name&gt;</code></p>
      </form>}
      {action === "re-enroll" && <ConfirmPanel title="Re-enroll this Spark?" consequence="Its current agent credentials are replaced. The Spark keeps its name and recipes but must run the new installer command to reconnect." confirmLabel="Create re-enrollment grant" command={`vonkctl fleet re-enroll ${id} --yes`} busy={busy} onConfirm={() => void reenroll()} onCancel={() => setAction(null)}/>}
      {action === "remove" && <ConfirmPanel title="Remove this Spark?" consequence="It leaves the fleet and its agent credentials are revoked. Enrolling it again needs a new grant. This cannot be undone." confirmLabel="Remove Spark" typeName={node.display_name} command={`vonkctl fleet remove ${id} --yes`} busy={busy} onConfirm={() => void remove()} onCancel={() => setAction(null)}/>}
      {grant && <EnrollmentGrant api={api} grant={grant}/>}
      {logs && <div className="spark-logs"><h3>Recent logs</h3>{logs.entries.length === 0 ? <p>No log lines retained for this Spark yet. Try again after it reports activity.</p> : <ol>{logs.entries.map(entry => <li key={entry.evidence_id}><Time value={entry.observed_at}/> <StatusPill tone={entry.level === "error" ? "danger" : entry.level === "warning" ? "warning" : "neutral"}>{entry.level}</StatusPill> <small>{entry.source}</small> {entry.message}</li>)}</ol>}<p>CLI: <code>vonkctl fleet loginfo {id}</code></p></div>}
    </>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
