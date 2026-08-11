import {useState} from "react";
import type {
  ControlApi,
  FleetResponse,
  NodeSummary,
  ReconciliationPlan as ReconciliationPlanModel,
} from "../api/types";

const COLLECTION_PAGE_SIZE = 10;
const MAX_IDENTIFIER = 160;
const MAX_PATH = 256;
const MAX_MESSAGE = 512;

function bounded(value: string, maximum = MAX_IDENTIFIER): string {
  return value.length > maximum ? `${value.slice(0, maximum)}…` : value;
}

function Pages<T>({
  items,
  label,
  render,
}: {
  items: readonly T[];
  label: string;
  render: (item: T, index: number) => React.ReactNode;
}) {
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(items.length / COLLECTION_PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const start = safePage * COLLECTION_PAGE_SIZE;
  const visible = items.slice(start, start + COLLECTION_PAGE_SIZE);
  const end = start + visible.length;

  return <>
    <p>{items.length === 0 ? `Showing ${label} 0 of 0` : `Showing ${label} ${start + 1}–${end} of ${items.length}`}</p>
    {visible.map((item, index) => render(item, start + index))}
    {pageCount > 1 && <div className="pagination">
      <button type="button" aria-label={`Previous ${label}`} disabled={safePage === 0} onClick={() => setPage(current => Math.max(0, current - 1))}>Previous</button>
      <button type="button" aria-label={`Next ${label}`} disabled={safePage === pageCount - 1} onClick={() => setPage(current => Math.min(pageCount - 1, current + 1))}>Next</button>
    </div>}
  </>;
}

function targetGate(node: NodeSummary | undefined, fleetCommit: string, planCommit: string): string[] {
  if (!node) return ["node missing from live fleet"];
  const reasons: string[] = [];
  if (fleetCommit !== planCommit) reasons.push("fleet commit does not match plan");
  if (node.healthy !== true) reasons.push(node.healthy === false ? "unavailable" : "health unknown");
  if (node.stale) reasons.push("stale");
  if (!node.agent_online) reasons.push("agent offline");
  if (node.agent_state !== "active") reasons.push(`agent ${bounded(node.agent_state)}`);
  if (node.compatibility !== "supported") reasons.push("agent compatibility failed");
  return reasons;
}

function planIntegrityReasons(
  plan: ReconciliationPlanModel,
  fleet: FleetResponse,
): string[] {
  const reasons: string[] = [];
  if (fleet.commit !== plan.commit) reasons.push("fleet commit does not match plan");
  if (fleet.evidence_digest !== plan.fleet_evidence_digest) reasons.push("fleet evidence does not match plan");
  const range = plan.agent_protocol_range;
  if (range.length !== 2
      || !range.every(value => Number.isInteger(value) && value > 0)
      || range[0] > range[1]) {
    reasons.push("agent protocol range is invalid");
  }
  if (plan.operation_graph.base_commit !== plan.commit) reasons.push("operation graph commit does not match plan");
  const targetSet = new Set(plan.targets);
  const graphTargetSet = new Set(plan.operation_graph.targets);
  if (targetSet.size !== plan.targets.length) reasons.push("plan targets contain duplicates");
  if (graphTargetSet.size !== plan.operation_graph.targets.length) reasons.push("operation graph targets contain duplicates");
  if (plan.targets.length !== plan.operation_graph.targets.length
      || plan.targets.some((target, index) => plan.operation_graph.targets[index] !== target)) {
    reasons.push("operation graph targets do not match plan targets");
  }
  if (plan.operation_graph.nodes.some(operation => !targetSet.has(operation.node_id))) {
    reasons.push("operation graph contains a non-target node");
  }
  return reasons;
}

function routeAddress(
  route: ReconciliationPlanModel["routes"][string],
  fleetById: ReadonlyMap<string, NodeSummary>,
): string {
  const node = fleetById.get(route.entrypoint_node_id);
  const entrypoint = node?.display_name ?? route.entrypoint_node_id;
  return bounded(`${route.scheme}://${entrypoint}:${route.port}${route.path}`, MAX_PATH);
}

export function ReconciliationPlan({
  api,
  fleet,
  plan,
}: {
  api: ControlApi;
  fleet: FleetResponse;
  plan: ReconciliationPlanModel;
}) {
  const [confirmation, setConfirmation] = useState("");
  const [applying, setApplying] = useState(false);
  const [rejected, setRejected] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const fleetById = new Map(fleet.nodes.map(node => [node.id, node]));
  const targets = plan.targets.map(nodeId => ({nodeId, node: fleetById.get(nodeId)}));
  const integrityReasons = planIntegrityReasons(plan, fleet);
  const protocolRangeValid = !integrityReasons.includes("agent protocol range is invalid");
  const blocked = integrityReasons.length > 0
    || targets.some(({node}) => targetGate(node, fleet.commit, plan.commit).length > 0);
  const operations = plan.operation_graph.nodes;
  const placements = Object.entries(plan.placements);
  const releases = Object.entries(plan.releases);
  const routes = Object.entries(plan.routes);
  const inputs = Object.entries(plan.input_digests);

  async function apply() {
    if (blocked || confirmation !== plan.digest || applying) return;
    setApplying(true);
    setError("");
    setStatus("");
    try {
      const latestFleet = await api.fleet();
      if (latestFleet.evidence_digest !== plan.fleet_evidence_digest
          || planIntegrityReasons(plan, latestFleet).length > 0) {
        setConfirmation("");
        setRejected(true);
        setError("Fleet acceptance evidence changed. Preview a new plan before retrying.");
        return;
      }
      const accepted = await api.applyReconciliation(plan.digest, plan.fleet_evidence_digest);
      setConfirmation("");
      setStatus(`Plan accepted as job ${bounded(accepted.job_id)} (${bounded(accepted.state)}).`);
    } catch (value) {
      const message = value instanceof Error ? value.message : "Plan apply failed";
      setConfirmation("");
      setRejected(true);
      setError(bounded(`${message}. Preview a new plan before retrying.`, MAX_MESSAGE));
    } finally {
      setApplying(false);
    }
  }

  return <section className="plan-review" aria-labelledby="plan-review-heading">
    <h3 id="plan-review-heading">Exact reconciliation plan</h3>
    <dl className="evidence-grid compact">
      <div><dt>Repository commit</dt><dd><code>{bounded(plan.commit)}</code></dd></div>
      <div><dt>Plan digest</dt><dd><code>{bounded(plan.digest)}</code></dd></div>
      <div><dt>Reconciliation</dt><dd><code>{bounded(plan.reconciliation_id)}</code></dd></div>
      <div><dt>Agent compatibility</dt><dd>{protocolRangeValid ? `Agent protocol ${plan.agent_protocol_range[0]}–${plan.agent_protocol_range[1]}` : "Blocked: invalid agent protocol range"}</dd></div>
    </dl>

    <section aria-labelledby="target-gates-heading">
      <h4 id="target-gates-heading">Affected nodes and acceptance gates</h4>
      <Pages items={targets} label="affected nodes" render={({nodeId, node}, index) => {
        const reasons = targetGate(node, fleet.commit, plan.commit);
        return <div className="table-scroll" key={`${nodeId}-${index}`}><table aria-label={`Acceptance gate for ${bounded(nodeId)}`}>
          <thead><tr><th scope="col">Node</th><th scope="col">Agent compatibility</th><th scope="col">Acceptance gate</th></tr></thead>
          <tbody><tr><th scope="row"><code>{bounded(nodeId)}</code><small>{bounded(node?.display_name ?? "Unknown node")}</small></th><td>{bounded(node?.compatibility ?? "unknown")}</td><td><span className={`status ${reasons.length === 0 ? "good" : "unknown"}`}>{reasons.length === 0 ? "Ready and compatible" : `Blocked: ${reasons.join(", ")}`}</span></td></tr></tbody>
        </table></div>;
      }}/>
    </section>

    <section aria-labelledby="operations-heading">
      <h4 id="operations-heading">Stop, start, and verification operations</h4>
      <Pages items={operations} label="operations" render={operation => <article className="operation-card" key={operation.operation_id}>
        <h5>{bounded(operation.kind)} <code>{bounded(operation.operation_id)}</code></h5>
        <p>Node <code>{bounded(operation.node_id)}</code> · workload <code>{bounded(operation.workload_id)}</code></p>
        <p>Payload <code>{bounded(operation.payload_digest)}</code>{operation.compensation_kind ? ` · compensation ${bounded(operation.compensation_kind)}` : ""}</p>
        <strong>Dependencies</strong>
        <Pages items={operation.dependencies} label={`dependencies for ${bounded(operation.operation_id)}`} render={dependency => <p key={dependency}><code>{bounded(dependency)}</code></p>}/>
      </article>}/>
    </section>

    <section aria-labelledby="placements-heading">
      <h4 id="placements-heading">Exact placement</h4>
      <Pages items={placements} label="placements" render={([workload, nodes]) => <article className="operation-card" key={workload}>
        <h5>{bounded(workload)}</h5>
        <Pages items={nodes} label={`placement nodes for ${bounded(workload)}`} render={node => <p key={node}><code>{bounded(node)}</code></p>}/>
      </article>}/>
    </section>

    <section aria-labelledby="releases-heading">
      <h4 id="releases-heading">Immutable releases</h4>
      <Pages items={releases} label="releases" render={([workload, release]) => <article className="operation-card" key={workload}>
        <h5>{bounded(workload)}</h5>
        <dl className="evidence-grid compact">
          <div><dt>Definition hash</dt><dd><code>{bounded(release.definition_hash)}</code></dd></div>
          <div><dt>Manifest</dt><dd><code>{bounded(release.manifest_path, MAX_PATH)}</code></dd></div>
          <div><dt>Manifest SHA-256</dt><dd><code>{bounded(release.manifest_sha256)}</code></dd></div>
          <div><dt>OCI manifest</dt><dd><code>{bounded(release.release_request.oci_manifest_digest)}</code></dd></div>
          <div><dt>Target digest</dt><dd><code>{bounded(release.release_request.target_digest)}</code></dd></div>
          <div><dt>Provenance digest</dt><dd><code>{bounded(release.release_request.provenance_digest)}</code></dd></div>
          <div><dt>Prepare profile</dt><dd><code>{bounded(release.workload_requests.prepare.profile_digest)}</code></dd></div>
          <div><dt>Start preparation</dt><dd><code>{bounded(release.workload_requests.start.preparation_digest)}</code></dd></div>
          <div><dt>Verify expected</dt><dd><code>{bounded(release.workload_requests.verify.expected_digest)}</code></dd></div>
        </dl>
      </article>}/>
    </section>

    <section aria-labelledby="routes-heading">
      <h4 id="routes-heading">Route maintenance</h4>
      <Pages items={routes} label="routes" render={([alias, route]) => <article className="operation-card" key={alias}>
        <h5>{bounded(alias)}</h5>
        <p>{routeAddress(route, fleetById)}</p>
        <p>Workload <code>{bounded(route.workload_id)}</code> · quota {route.quota.requests_per_minute} requests/min, {route.quota.tokens_per_minute} tokens/min · <code>{bounded(route.quota_digest)}</code></p>
        <Pages items={route.nodes} label={`route nodes for ${bounded(alias)}`} render={node => <p key={node}><code>{bounded(node)}</code></p>}/>
      </article>}/>
    </section>

    <section aria-labelledby="inputs-heading">
      <h4 id="inputs-heading">Pinned acceptance inputs</h4>
      <Pages items={inputs} label="input digests" render={([name, inputDigest]) => <p key={name}>{bounded(name)}: <code>{bounded(inputDigest)}</code></p>}/>
    </section>

    {blocked && <p role="alert">This plan cannot be applied because authoritative plan, fleet, node, or agent protocol acceptance checks failed closed.</p>}
    {error && <p role="alert">{error}</p>}
    {status && <p role="status">{status}</p>}
    <label>Type the exact plan digest to confirm apply
      <input autoComplete="off" disabled={rejected} maxLength={128} value={confirmation} onChange={event => setConfirmation(event.target.value)}/>
    </label>
    <button type="button" disabled={blocked || rejected || applying || confirmation !== plan.digest} onClick={() => void apply()}>Apply exact plan</button>
  </section>;
}
