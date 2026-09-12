import {useMemo, useState} from "react";
import type {ControlApi, VisualFleetNode} from "../api/types";
import {useFleetStream} from "../hooks/use-fleet-stream";
import {formatBytes, nodeDisplayName, nodeOperationalState, nodeSecondaryName} from "../lib/fleet";
import {StatusPill} from "../components/status-pill";

function stateTone(state: ReturnType<typeof nodeOperationalState>): "healthy" | "warning" | "danger" {
  return state === "live" ? "healthy" : state === "offline" ? "danger" : "warning";
}

function memoryLabel(node: VisualFleetNode): string {
  const available = node.telemetry?.sample.memory_available_bytes;
  return typeof available === "number" ? `${formatBytes(available)} free` : "Not reported";
}

export function FleetPage({api}: {api: ControlApi; onBusyChange?(busy: boolean): void}) {
  const fleet = useFleetStream(api);
  const [query, setQuery] = useState("");
  const visibleNodes = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return (fleet.snapshot?.nodes ?? []).filter(node => !needle || `${nodeDisplayName(node)} ${nodeSecondaryName(node) ?? ""}`.toLocaleLowerCase().includes(needle));
  }, [fleet.snapshot?.nodes, query]);

  return <div className="fleet-page">
    <header className="fleet-command-header"><div className="fleet-command-title"><h1>Fleet</h1><p>Every enrolled Spark, its health, and what is running there.</p></div><div className="fleet-command-actions"><a className="button" href="/library">Open Library</a><button type="button" className="button secondary" onClick={fleet.retry}>Refresh</button></div></header>
    {fleet.snapshot && <section className="fleet-command-summary" aria-label="Fleet summary">
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.length}</strong><span>Sparks</span></div>
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.filter(node => nodeOperationalState(node, fleet.now) === "live").length}</strong><span>Live</span></div>
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.reduce((count, node) => count + node.loaded.filter(item => item.healthy).length, 0)}</strong><span>Running recipes</span></div>
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.reduce((count, node) => count + node.installed.filter(item => item.complete).length, 0)}</strong><span>Installed recipes</span></div>
    </section>}
    {fleet.snapshot && fleet.snapshot.nodes.length > 0 && <section className="fleet-discovery" aria-label="Find a Spark"><label className="fleet-search"><span>Find a Spark</span><input type="search" value={query} placeholder="Friendly name or technical name" onChange={event => setQuery(event.currentTarget.value)}/></label><span role="status">Showing {visibleNodes.length} of {fleet.snapshot.nodes.length}</span></section>}
    {fleet.loading && !fleet.snapshot && <section className="fleet-loading" role="status"><h2>Loading Fleet</h2><p>Reading the current Spark state…</p></section>}
    {fleet.error && !fleet.snapshot && <section className="fleet-error" role="alert"><h2>Fleet unavailable</h2><p>{fleet.error}</p><button type="button" className="button" onClick={fleet.retry}>Retry</button></section>}
    {fleet.snapshot?.nodes.length === 0 && <section className="fleet-empty"><h2>No Sparks enrolled</h2><p>Enroll a Spark with <code>vonkctl fleet enroll</code>.</p></section>}
    {visibleNodes.length > 0 && <section className="fleet-compact" aria-label="Fleet table"><div className="fleet-table-scroll"><table><caption className="sr-only">Fleet health, capacity, and workloads</caption><thead><tr><th scope="col">Spark</th><th scope="col">Health</th><th scope="col">Memory</th><th scope="col">GPU</th><th scope="col">Recipes</th></tr></thead><tbody>{visibleNodes.map(node => { const state = nodeOperationalState(node, fleet.now); const name = nodeDisplayName(node); return <tr key={node.id}><th scope="row"><strong>{name}</strong>{nodeSecondaryName(node) && <small>{nodeSecondaryName(node)}</small>}</th><td><StatusPill tone={stateTone(state)}>{state}</StatusPill></td><td>{memoryLabel(node)}</td><td>{typeof node.telemetry?.sample.gpu_utilization_percent === "number" ? `${node.telemetry.sample.gpu_utilization_percent.toFixed(1)}%` : "Not reported"}</td><td><strong>{node.loaded.filter(item => item.healthy).length} running</strong><small>{node.installed.filter(item => item.complete).length} installed</small></td></tr>; })}</tbody></table></div></section>}
  </div>;
}
