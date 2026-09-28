import {useMemo, useState, type FormEvent} from "react";
import type {ControlApi, EnrollmentGrantResponse, VisualFleetNode} from "../api/types";
import {useFleetStream} from "../hooks/use-fleet-stream";
import {formatBytes, nodeDisplayName, nodeOperationalState, nodeSecondaryName} from "../lib/fleet";
import {StatusPill} from "../components/status-pill";
import {CopyButton} from "../components/copy-button";

function stateTone(state: ReturnType<typeof nodeOperationalState>): "healthy" | "warning" | "danger" {
  return state === "live" ? "healthy" : state === "offline" ? "danger" : "warning";
}

function memoryLabel(node: VisualFleetNode): string {
  const available = node.telemetry?.sample.memory_available_bytes;
  return typeof available === "number" ? `${formatBytes(available)} free` : "Not reported";
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

export function FleetPage({api}: {api: ControlApi; onBusyChange?(busy: boolean): void}) {
  const fleet = useFleetStream(api);
  const [query, setQuery] = useState("");
  const [enrolling, setEnrolling] = useState(false);
  const [sparkName, setSparkName] = useState("");
  const [grant, setGrant] = useState<EnrollmentGrantResponse | null>(null);
  const [enrollmentError, setEnrollmentError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const visibleNodes = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return (fleet.snapshot?.nodes ?? []).filter(node => !needle || `${nodeDisplayName(node)} ${nodeSecondaryName(node) ?? ""}`.toLocaleLowerCase().includes(needle));
  }, [fleet.snapshot?.nodes, query]);

  async function createGrant(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const name = sparkName.trim();
    if (!name || submitting) return;
    setSubmitting(true);
    setEnrollmentError(null);
    try {
      const response = await api.enrollFleetNode({name, request_key: crypto.randomUUID(), ttl_seconds: 900});
      if (response.action !== "enroll" || !response.grant) throw new Error("Controller did not return an enrollment grant");
      setGrant(response.grant);
    } catch (error) {
      setEnrollmentError(error instanceof Error ? error.message : "Could not create an enrollment grant");
    } finally {
      setSubmitting(false);
    }
  }

  const enrollmentCommand = grant
    ? `curl -fsSL ${grant.installer_url} | ${grant.controller_address ? `VONK_CONTROLLER_ADDRESS=${shellQuote(grant.controller_address)} ` : ""}VONK_ENROLLMENT_URL=${shellQuote(grant.enrollment_endpoint)} VONK_CONTROLLER_CA_SHA256=${shellQuote(grant.ca_fingerprint)} sh -s -- --enroll`
    : "";

  return <div className="fleet-page">
    <header className="fleet-command-header"><div className="fleet-command-title"><h1>Fleet</h1><p>Every enrolled Spark, its health, and what is running there.</p></div><div className="fleet-command-actions"><button type="button" className="button" onClick={() => {setEnrolling(value => !value); setGrant(null); setEnrollmentError(null);}}>Enroll Spark</button><a className="button" href="/library">Open Library</a><button type="button" className="button secondary" onClick={fleet.retry}>Refresh</button></div></header>
    {enrolling && <section className="fleet-enrollment" aria-labelledby="fleet-enrollment-heading">
      <div><h2 id="fleet-enrollment-heading">Enroll a Spark</h2><p>Create a one-use grant that expires in 15 minutes.</p></div>
      {!grant && <form onSubmit={event => void createGrant(event)}><label htmlFor="fleet-spark-name">Spark name</label><input id="fleet-spark-name" required maxLength={80} value={sparkName} onChange={event => setSparkName(event.currentTarget.value)} placeholder="e.g. Spark at home"/><button type="submit" className="button" disabled={submitting}>{submitting ? "Creating grant…" : "Create enrollment grant"}</button></form>}
      {enrollmentError && <p role="alert">{enrollmentError}</p>}
      {grant && <div className="fleet-enrollment-result"><p><strong>Grant expires:</strong> <time dateTime={grant.expires_at}>{new Date(grant.expires_at).toLocaleString()}</time></p><p>Run this command on the Spark. The installer will ask for the one-use pairing token.</p><pre><code>{enrollmentCommand}</code></pre><CopyButton label="command" value={enrollmentCommand}/><p><label htmlFor="fleet-enrollment-token">One-use pairing token</label></p><input id="fleet-enrollment-token" readOnly value={grant.token} aria-label="One-use pairing token"/><CopyButton label="pairing token" value={grant.token}/><button type="button" className="button secondary" onClick={() => {setGrant(null); setSparkName("");}}>Create another grant</button></div>}
    </section>}
    {fleet.snapshot && <section className="fleet-command-summary" aria-label="Fleet summary">
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.length}</strong><span>Sparks</span></div>
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.filter(node => nodeOperationalState(node, fleet.now) === "live").length}</strong><span>Live</span></div>
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.reduce((count, node) => count + node.loaded.filter(item => item.healthy).length, 0)}</strong><span>Running recipes</span></div>
      <div className="fleet-command-fact"><strong>{fleet.snapshot.nodes.reduce((count, node) => count + node.installed.filter(item => item.complete).length, 0)}</strong><span>Installed recipes</span></div>
    </section>}
    {fleet.snapshot && fleet.snapshot.nodes.length > 0 && <section className="fleet-discovery" aria-label="Find a Spark"><label className="fleet-search"><span>Find a Spark</span><input type="search" value={query} placeholder="Friendly name or technical name" onChange={event => setQuery(event.currentTarget.value)}/></label><span role="status">Showing {visibleNodes.length} of {fleet.snapshot.nodes.length}</span></section>}
    {fleet.loading && !fleet.snapshot && <section className="fleet-loading" role="status"><h2>Loading Fleet</h2><p>Reading the current Spark state…</p></section>}
    {fleet.error && !fleet.snapshot && <section className="fleet-error" role="alert"><h2>Fleet unavailable</h2><p>{fleet.error}</p><button type="button" className="button" onClick={fleet.retry}>Retry</button></section>}
    {fleet.snapshot?.nodes.length === 0 && <section className="fleet-empty"><h2>No Sparks enrolled</h2><p>Use <strong>Enroll Spark</strong> to create a one-use pairing grant.</p></section>}
    {visibleNodes.length > 0 && <section className="fleet-compact" aria-label="Fleet table"><div className="fleet-table-scroll"><table><caption className="sr-only">Fleet health, capacity, and workloads</caption><thead><tr><th scope="col">Spark</th><th scope="col">Health</th><th scope="col">Memory</th><th scope="col">GPU</th><th scope="col">Recipes</th></tr></thead><tbody>{visibleNodes.map(node => { const state = nodeOperationalState(node, fleet.now); const name = nodeDisplayName(node); return <tr key={node.id}><th scope="row"><strong>{name}</strong>{nodeSecondaryName(node) && <small>{nodeSecondaryName(node)}</small>}</th><td><StatusPill tone={stateTone(state)}>{state}</StatusPill></td><td>{memoryLabel(node)}</td><td>{typeof node.telemetry?.sample.gpu_utilization_percent === "number" ? `${node.telemetry.sample.gpu_utilization_percent.toFixed(1)}%` : "Not reported"}</td><td><strong>{node.loaded.filter(item => item.healthy).length} running</strong><small>{node.installed.filter(item => item.complete).length} installed</small></td></tr>; })}</tbody></table></div></section>}
  </div>;
}
