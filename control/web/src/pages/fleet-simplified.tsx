import {useEffect, useMemo, useState, type FormEvent} from "react";
import type {ControlApi, EnrollmentGrantResponse, VisualFleetNode} from "../api/types";
import {useFleetStream} from "../hooks/use-fleet-stream";
import {formatBytes, nodeDiskFreeBytes, nodeDisplayName, nodeMemory, nodeSecondaryName, nodeStatus, type NodeStatus} from "../lib/fleet";
import {SortHeader, useSort} from "../components/sort-header";
import {StatusPill} from "../components/status-pill";
import {ConfirmPanel} from "../components/confirm-panel";
import {EnrollmentGrant} from "../components/enrollment-grant";
import {SparkDetail} from "../components/spark-detail";
import {safeErrorText} from "../lib/error-display";

function statusTone(status: NodeStatus): "healthy" | "warning" | "danger" {
  return status === "online" ? "healthy" : status === "offline" ? "danger" : "warning";
}

// Problems first when sorted ascending.
const STATUS_RANK: Record<NodeStatus, number> = {offline: 0, "needs attention": 1, online: 2};

function memoryLabel(node: VisualFleetNode): string {
  const memory = nodeMemory(node);
  return memory ? `${memory.usedPercent}% of ${formatBytes(memory.totalBytes)}` : "Not reported";
}

function selectedSpark(): string | null {
  return new URLSearchParams(location.search).get("spark");
}

export function FleetPage({api}: {api: ControlApi; onBusyChange?(busy: boolean): void}) {
  const fleet = useFleetStream(api);
  const [query, setQuery] = useState("");
  const [enrolling, setEnrolling] = useState(false);
  const [sparkName, setSparkName] = useState("");
  const [grant, setGrant] = useState<EnrollmentGrantResponse | null>(null);
  const [enrollmentError, setEnrollmentError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [spark, setSpark] = useState(selectedSpark);
  const [upgrading, setUpgrading] = useState(false);
  const [upgradeState, setUpgradeState] = useState<{busy: boolean; message: string | null}>({busy: false, message: null});
  useEffect(() => {
    const listener = () => setSpark(selectedSpark());
    addEventListener("popstate", listener);
    return () => removeEventListener("popstate", listener);
  }, []);
  function selectSpark(id: string | null) {
    const url = new URL(location.href);
    if (id) url.searchParams.set("spark", id); else url.searchParams.delete("spark");
    history.pushState(null, "", `${url.pathname}${url.search}`);
    setSpark(id);
  }
  async function upgradeAll() {
    setUpgradeState({busy: true, message: null});
    try {
      const response = await api.upgradeFleet(true, [], crypto.randomUUID());
      setUpgradeState({busy: false, message: `Upgrade ${response.state}${response.operation_id ? `. Follow it in Activity (${response.operation_id}).` : "."}`});
      setUpgrading(false);
    } catch (error) {
      setUpgradeState({busy: false, message: safeErrorText(error instanceof Error ? error.message : "Could not start the agent upgrade")});
    }
  }
  const visibleNodes = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return (fleet.snapshot?.nodes ?? []).filter(node => !needle || `${nodeDisplayName(node)} ${nodeSecondaryName(node) ?? ""}`.toLocaleLowerCase().includes(needle));
  }, [fleet.snapshot?.nodes, query]);

  const {sorted, sort, toggle} = useSort<VisualFleetNode, "name" | "status" | "memory" | "disk">(visibleNodes, {key: "name", descending: false}, {
    name: node => nodeDisplayName(node).toLocaleLowerCase(),
    status: node => STATUS_RANK[nodeStatus(node, fleet.now).status],
    memory: node => nodeMemory(node)?.usedPercent ?? null,
    disk: node => nodeDiskFreeBytes(node),
  });

  async function createGrant(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const name = sparkName.trim();
    if (!name || submitting) return;
    setSubmitting(true);
    setEnrollmentError(null);
    try {
      const response = await api.enrollFleetNode({name, request_key: crypto.randomUUID()});
      if (response.action !== "enroll" || !response.grant) throw new Error("Controller did not return an enrollment grant");
      setGrant(response.grant);
    } catch (error) {
      setEnrollmentError(error instanceof Error ? error.message : "Could not create an enrollment grant");
    } finally {
      setSubmitting(false);
    }
  }

  return <div className="fleet-page">
    <header className="fleet-command-header"><div className="fleet-command-title"><h1>Fleet</h1><p>Every enrolled Spark, its health, and what is running there.</p></div><div className="fleet-command-actions"><button type="button" className="button" onClick={() => {setEnrolling(value => !value); setGrant(null); setEnrollmentError(null);}}>Enroll Spark</button><button type="button" className="button secondary" onClick={() => setUpgrading(value => !value)}>Upgrade agents</button><button type="button" className="button secondary" onClick={fleet.retry}>Refresh</button></div></header>
    {enrolling && <section className="fleet-enrollment" aria-labelledby="fleet-enrollment-heading">
      <div><h2 id="fleet-enrollment-heading">Enroll a Spark</h2><p>Create a one-use grant that expires in 15 minutes.</p></div>
      {!grant && <form onSubmit={event => void createGrant(event)}><label htmlFor="fleet-spark-name">Spark name</label><input id="fleet-spark-name" required maxLength={80} value={sparkName} onChange={event => setSparkName(event.currentTarget.value)} placeholder="e.g. Spark at home"/><button type="submit" className="button" disabled={submitting}>{submitting ? "Creating grant…" : "Create enrollment grant"}</button></form>}
      {enrollmentError && <p role="alert">{enrollmentError}</p>}
      {grant && <><EnrollmentGrant api={api} grant={grant}/><button type="button" className="button secondary" onClick={() => {setGrant(null); setSparkName("");}}>Create another grant</button></>}
    </section>}
    {upgrading && <ConfirmPanel title="Upgrade every Spark agent?" consequence="Agents upgrade one Spark at a time and restart. Running recipes keep serving unless a Spark reports a problem." confirmLabel="Upgrade agents" command="vonkctl fleet upgrade --all --yes" busy={upgradeState.busy} onConfirm={() => void upgradeAll()} onCancel={() => setUpgrading(false)}/>}
    {upgradeState.message && <p role="status">{upgradeState.message}</p>}
    {fleet.snapshot && <section className="fleet-command-summary" aria-label="Fleet summary">
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.length}</strong><span>Sparks</span></div>
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.filter(node => node.connection.online_state === "online").length}</strong><span>Online</span></div>
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.filter(node => nodeStatus(node, fleet.now).status !== "online").length}</strong><span>Need attention</span></div>
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.reduce((count, node) => count + node.loaded.filter(item => item.healthy).length, 0)}</strong><span>Running recipes</span></div>
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.reduce((count, node) => count + node.installed.filter(item => item.complete).length, 0)}</strong><span>Installed recipes</span></div>
    </section>}
    {fleet.snapshot && fleet.snapshot.nodes.length > 0 && <section className="fleet-discovery" aria-label="Find a Spark"><label className="fleet-search"><span>Find a Spark</span><input type="search" value={query} placeholder="Friendly name or technical name" onChange={event => setQuery(event.currentTarget.value)}/></label><span role="status">Showing {visibleNodes.length} of {fleet.snapshot.nodes.length}</span></section>}
    {fleet.loading && !fleet.snapshot && <section className="fleet-loading" role="status"><h2>Loading Fleet</h2><p>Reading the current Spark state…</p></section>}
    {fleet.error && !fleet.snapshot && <section className="fleet-error" role="alert"><h2>Fleet unavailable</h2><p>{fleet.error}</p><button type="button" className="button" onClick={fleet.retry}>Retry</button></section>}
    {fleet.snapshot?.nodes.length === 0 && <section className="fleet-empty"><h2>No Sparks enrolled</h2><p>Use <strong>Enroll Spark</strong> to create a one-use pairing grant.</p></section>}
    {visibleNodes.length > 0 && <section className="fleet-compact" aria-label="Fleet table"><div className="fleet-table-scroll"><table><caption className="sr-only">Fleet health, capacity, and workloads</caption><thead><tr>
      <SortHeader column="name" label="Spark" sort={sort} onSort={toggle}/><SortHeader column="status" label="Status" sort={sort} onSort={toggle}/><SortHeader column="memory" label="Memory used" sort={sort} onSort={toggle}/><SortHeader column="disk" label="Disk free" sort={sort} onSort={toggle}/><th scope="col">GPU</th><th scope="col">Recipes</th>
    </tr></thead><tbody>{sorted.map(node => { const {status, reasons} = nodeStatus(node, fleet.now); const name = nodeDisplayName(node); const disk = nodeDiskFreeBytes(node); return <tr key={node.id}><th scope="row"><a href={`?spark=${encodeURIComponent(node.id)}`} aria-current={spark === node.id ? "true" : undefined} onClick={event => {event.preventDefault(); selectSpark(node.id);}}><strong>{name}</strong></a>{nodeSecondaryName(node) && <small>{nodeSecondaryName(node)}</small>}</th><td><StatusPill tone={statusTone(status)}>{status}</StatusPill>{reasons.length > 0 && <small title={reasons.join("\n")}>{reasons[0]}{reasons.length > 1 ? ` +${reasons.length - 1} more` : ""}</small>}</td><td>{memoryLabel(node)}</td><td>{disk === null ? "Not reported" : formatBytes(disk)}</td><td>{typeof node.telemetry?.sample.gpu_utilization_percent === "number" ? `${node.telemetry.sample.gpu_utilization_percent.toFixed(1)}%` : "Not reported"}</td><td><strong>{node.loaded.filter(item => item.healthy).length} running</strong><small>{node.installed.filter(item => item.complete).length} installed</small></td></tr>; })}</tbody></table></div></section>}
    {spark && <SparkDetail api={api} id={spark} onClose={() => selectSpark(null)}/>}
  </div>;
}
