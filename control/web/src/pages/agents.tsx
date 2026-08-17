import {useCallback, useEffect, useId, useRef, useState} from "react";
import type {
  AgentSummary,
  ControlApi,
  EnrollmentGrantResponse,
  EnrollmentSummary,
  FleetEvidenceResponse,
} from "../api/types";
import {EnrollmentReview} from "../components/enrollment-review";

function sameOriginPath(configured: string | undefined, fallback: string): string {
  try {
    const url = new URL(configured || fallback, location.origin);
    if (url.origin !== location.origin) return fallback;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return fallback;
  }
}

const AGENT_PAGE_SIZE = 20;
const CAPABILITY_PAGE_SIZE = 3;
const MAX_COMPATIBILITY_LENGTH = 64;
const MAX_CAPABILITY_LENGTH = 80;
const MAX_MESSAGE_LENGTH = 512;
const MAX_NODE_ID_LENGTH = 36;
const MAX_STATE_LENGTH = 64;
const MAX_TIMESTAMP_LENGTH = 64;
const MAX_TOKEN_LENGTH = 64;

function boundedText(value: string, maxLength: number): string {
  return value.length > maxLength
    ? `${value.slice(0, maxLength)}…`
    : value;
}

function boundedValueOrDash(
  value: string | number | null | undefined,
  maxLength: number,
): string {
  if (value === null || value === undefined || value === "") return "—";
  return boundedText(String(value), maxLength);
}

function implementationLabel(value: string): string {
  if (value === "rust") return "Rust agent";
  if (value === "python") return "Python agent";
  return "Agent activation pending";
}

function certificateSnapshot(agent: AgentSummary): string {
  return [agent.node_id, agent.state, agent.certificate_expires_at, agent.protocol_version].join("\u0000");
}

function enrollmentEvidenceSnapshot(enrollment: EnrollmentSummary): string {
  return [
    enrollment.id,
    enrollment.node_id,
    enrollment.state,
    enrollment.host_key_fingerprint,
    enrollment.hardware_fingerprint,
    enrollment.agent_digest,
    enrollment.csr_public_key_fingerprint,
    enrollment.boot_id,
    enrollment.created_at,
    enrollment.certificate_fingerprint,
    enrollment.certificate_serial,
  ].join("\u0000");
}

function Capabilities({agent}: {agent: AgentSummary}) {
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(agent.capabilities.length / CAPABILITY_PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const start = safePage * CAPABILITY_PAGE_SIZE;
  const visible = agent.capabilities.slice(start, start + CAPABILITY_PAGE_SIZE);
  const end = start + visible.length;

  if (agent.capabilities.length === 0) return <>—</>;

  return <div className="capability-list">
    <span role="status" aria-label={`Capability result count for ${boundedText(agent.node_id, MAX_NODE_ID_LENGTH)}`}>
      Capabilities {start + 1}–{end} of {agent.capabilities.length}
    </span>
    <span>{visible.map(value => boundedText(value, MAX_CAPABILITY_LENGTH)).join(", ")}</span>
    {pageCount > 1 && <div className="pagination">
      <button
        type="button"
        aria-label={`Previous capabilities for ${boundedText(agent.node_id, MAX_NODE_ID_LENGTH)}`}
        disabled={safePage === 0}
        onClick={() => setPage(current => Math.max(0, current - 1))}
      >Previous</button>
      <button
        type="button"
        aria-label={`Next capabilities for ${boundedText(agent.node_id, MAX_NODE_ID_LENGTH)}`}
        disabled={safePage === pageCount - 1}
        onClick={() => setPage(current => Math.min(pageCount - 1, current + 1))}
      >Next</button>
    </div>}
  </div>;
}

function CertificateControls({
  agent,
  disabled,
  onRevoke,
}: {
  agent: AgentSummary;
  disabled: boolean;
  onRevoke(nodeId: string): Promise<void>;
}) {
  const headingId = useId();
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const displayedNodeId = boundedText(agent.node_id, MAX_NODE_ID_LENGTH);

  async function revoke() {
    setBusy(true);
    try {
      await onRevoke(agent.node_id);
    } finally {
      setBusy(false);
    }
  }

  return <section className="certificate-controls" role="region" aria-labelledby={headingId}>
    <h4 id={headingId}>Certificate controls for {displayedNodeId}</h4>
    <p role="alert">Revocation immediately disconnects this node and cannot be undone. A new enrollment is required.</p>
    <label>Type {displayedNodeId} to confirm certificate revocation
      <input
        autoComplete="off"
        disabled={disabled}
        maxLength={MAX_NODE_ID_LENGTH}
        value={confirmation}
        onChange={event => setConfirmation(event.target.value)}
      />
    </label>
    <button
      className="danger"
      type="button"
      disabled={disabled || confirmation !== agent.node_id || busy}
      onClick={() => void revoke()}
    >Revoke node certificate</button>
  </section>;
}

export function AgentsPage({api}: {api: ControlApi}) {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [enrollments, setEnrollments] = useState<EnrollmentSummary[]>([]);
  const [fleet, setFleet] = useState<FleetEvidenceResponse>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [grant, setGrant] = useState<EnrollmentGrantResponse>();
  const [grantPending, setGrantPending] = useState(false);
  const [grantNodeId, setGrantNodeId] = useState("");
  const [grantTtl, setGrantTtl] = useState("300");
  const [agentPage, setAgentPage] = useState(0);
  const [dataRevision, setDataRevision] = useState(0);
  const grantRequest = useRef<{controller: AbortController; id: number} | undefined>(undefined);
  const grantRequestId = useRef(0);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [agentResult, enrollmentResult, fleetResult] = await Promise.all([
        api.agents(),
        api.enrollments(),
        api.fleetEvidence(),
      ]);
      setAgents(agentResult.agents);
      setAgentPage(0);
      setEnrollments(enrollmentResult.enrollments);
      setFleet(fleetResult);
      setDataRevision(current => current + 1);
    } catch (value) {
      setError(boundedText(
        value instanceof Error ? value.message : "Unable to load agent data",
        MAX_MESSAGE_LENGTH,
      ));
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => () => {
    grantRequestId.current += 1;
    grantRequest.current?.controller.abort();
    grantRequest.current = undefined;
  }, []);

  async function mutate(action: () => Promise<void>) {
    setError("");
    try {
      await action();
    } catch (value) {
      setError(value instanceof Error ? value.message : "Agent operation failed");
    }
  }

  async function createGrant(event: React.FormEvent) {
    event.preventDefault();
    if (grantPending || grant) return;
    const controller = new AbortController();
    const requestId = ++grantRequestId.current;
    grantRequest.current = {controller, id: requestId};
    setGrantPending(true);
    setError("");
    setStatus("");
    try {
      const created = await api.createEnrollmentGrant(
        grantNodeId,
        Number(grantTtl),
        controller.signal,
      );
      if (requestId !== grantRequestId.current) return;
      setGrant(created);
      setStatus(`One-time grant created for ${boundedText(created.node_id, MAX_NODE_ID_LENGTH)}`);
    } catch (value) {
      if (requestId !== grantRequestId.current || controller.signal.aborted) return;
      setError(boundedText(
        value instanceof Error ? value.message : "Enrollment grant creation failed",
        MAX_MESSAGE_LENGTH,
      ));
    } finally {
      if (requestId === grantRequestId.current) {
        grantRequest.current = undefined;
        setGrantPending(false);
      }
    }
  }

  async function createMigrationGrant(nodeId: string) {
    if (grantPending || grant) return;
    const controller = new AbortController();
    const requestId = ++grantRequestId.current;
    grantRequest.current = {controller, id: requestId};
    setGrantPending(true);
    setError("");
    setStatus("");
    try {
      const created = await api.createAgentMigrationGrant(
        nodeId,
        Number(grantTtl),
        controller.signal,
      );
      if (requestId !== grantRequestId.current) return;
      setGrant(created);
      setStatus(`Rust migration grant created for ${boundedText(created.node_id, MAX_NODE_ID_LENGTH)}`);
    } catch (value) {
      if (requestId !== grantRequestId.current || controller.signal.aborted) return;
      setError(boundedText(
        value instanceof Error ? value.message : "Migration grant creation failed",
        MAX_MESSAGE_LENGTH,
      ));
    } finally {
      if (requestId === grantRequestId.current) {
        grantRequest.current = undefined;
        setGrantPending(false);
      }
    }
  }

  async function approve(enrollmentId: string) {
    await mutate(async () => {
      const decision = await api.approveEnrollment(enrollmentId);
      setEnrollments(current => current.map(item => item.id === decision.id ? {...item, state: decision.state} : item));
      setStatus(`Enrollment for ${boundedText(decision.node_id, MAX_NODE_ID_LENGTH)} approved`);
    });
  }

  async function reject(enrollmentId: string, reason: string) {
    await mutate(async () => {
      const decision = await api.rejectEnrollment(enrollmentId, reason);
      setEnrollments(current => current.map(item => item.id === decision.id ? {
        ...item,
        rejection_reason: reason,
        state: decision.state,
      } : item));
      setStatus(`Enrollment for ${boundedText(decision.node_id, MAX_NODE_ID_LENGTH)} rejected`);
    });
  }

  async function revoke(nodeId: string) {
    await mutate(async () => {
      await api.revokeAgentNode(nodeId);
      setAgents(current => current.map(item => item.node_id === nodeId ? {
        ...item,
        certificate_expires_at: null,
        state: "revoked",
      } : item));
      setStatus(`Certificate for ${boundedText(nodeId, MAX_NODE_ID_LENGTH)} revoked`);
    });
  }

  function refresh() {
    setDataRevision(current => current + 1);
    grantRequestId.current += 1;
    grantRequest.current?.controller.abort();
    grantRequest.current = undefined;
    setGrantPending(false);
    setGrant(undefined);
    setStatus("");
    void load();
  }

  const compatibility = new Map(fleet?.nodes.map(node => [node.id, node.compatibility]));
  const agentPageCount = Math.max(1, Math.ceil(agents.length / AGENT_PAGE_SIZE));
  const safeAgentPage = Math.min(agentPage, agentPageCount - 1);
  const agentStart = safeAgentPage * AGENT_PAGE_SIZE;
  const visibleAgents = agents.slice(agentStart, agentStart + AGENT_PAGE_SIZE);
  const agentEnd = agentStart + visibleAgents.length;
  const litellmPath = sameOriginPath(import.meta.env.VITE_LITELLM_ADMIN_PATH, "/litellm/ui/");
  const grafanaPath = sameOriginPath(import.meta.env.VITE_GRAFANA_PATH, "/grafana/");

  return <>
    <div className="page-heading">
      <div>
        <h2>Agent enrollment and fleet</h2>
        <p>Review identity evidence, manage node certificates, and inspect bounded agent state.</p>
      </div>
      <button type="button" onClick={refresh}>Refresh agent data</button>
    </div>
    {loading && <p role="status">Loading agent data…</p>}
    {error && <p role="alert">{boundedText(error, MAX_MESSAGE_LENGTH)}</p>}
    {status && <p role="status">{boundedText(status, MAX_MESSAGE_LENGTH)}</p>}

    <section aria-labelledby="native-admin-heading" className="native-links">
      <h3 id="native-admin-heading">Native administration surfaces</h3>
      <p><a href={litellmPath}>LiteLLM Admin UI — keys, teams, and spend</a></p>
      <p><a href={grafanaPath}>Grafana — fleet dashboards</a></p>
      <p>These same-origin links remain protected by Caddy. Local PostgreSQL remains recipe and routing authority; LiteLLM records are a controller-managed projection.</p>
    </section>

    <section aria-labelledby="grant-heading">
      <h3 id="grant-heading">Create enrollment grant</h3>
      <form onSubmit={event => void createGrant(event)}>
        <label>Grant node ID
          <input
            maxLength={MAX_NODE_ID_LENGTH}
            required
            value={grantNodeId}
            onChange={event => setGrantNodeId(event.target.value)}
          />
        </label>
        <label>Grant lifetime in seconds
          <input required min="1" max="600" type="number" value={grantTtl} onChange={event => setGrantTtl(event.target.value)}/>
        </label>
        <button type="submit" disabled={grantPending || Boolean(grant)}>Create one-time grant</button>
      </form>
      {grantPending && <p role="status" aria-label="Enrollment grant request">Creating one-time enrollment grant…</p>}
      {grant && <div className="grant-secret" role="status" aria-label="One-time enrollment grant">
        <strong>Copy this token now. It will not be shown again.</strong>
        <span>{grant.purpose === "rust-migration" ? "Rust migration" : "New node"}</span>
        <code>{boundedText(grant.token, MAX_TOKEN_LENGTH)}</code>
        <span>Expires at {boundedText(grant.expires_at, MAX_TIMESTAMP_LENGTH)}</span>
        <button type="button" onClick={() => setGrant(undefined)}>Dismiss token</button>
      </div>}
    </section>

    <section aria-labelledby="agents-heading">
      <h3 id="agents-heading">Enrolled agents</h3>
      <p role="status" aria-label="Agent result count">
        {agents.length === 0
          ? "Showing agents 0 of 0"
          : `Showing agents ${agentStart + 1}–${agentEnd} of ${agents.length}`}
      </p>
      <div className="table-scroll"><table aria-label="Enrolled agents">
        <caption>Current bounded agent and certificate status</caption>
        <thead><tr>
          <th scope="col">Immutable node ID</th>
          <th scope="col">State and version</th>
          <th scope="col">Last seen</th>
          <th scope="col">Certificate expiry</th>
          <th scope="col">Compatibility</th>
          <th scope="col">Capabilities</th>
          <th scope="col">Migration action</th>
        </tr></thead>
        <tbody>{visibleAgents.map(agent => <tr key={agent.node_id}>
          <th scope="row"><code>{boundedText(agent.node_id, MAX_NODE_ID_LENGTH)}</code></th>
          <td>
            <span className="status">{boundedText(agent.state, MAX_STATE_LENGTH)}</span>
            <small>{implementationLabel(agent.agent_implementation)}</small>
            <small>Protocol {boundedValueOrDash(agent.protocol_version, MAX_STATE_LENGTH)}</small>
            {agent.migration_state === "required" && <strong role="status">Migration required</strong>}
          </td>
          <td>{boundedValueOrDash(agent.last_seen_at, MAX_TIMESTAMP_LENGTH)}<small>{agent.stale ? "Stale" : `${boundedValueOrDash(agent.last_seen_age_seconds, MAX_TIMESTAMP_LENGTH)} seconds ago`}</small></td>
          <td>{boundedValueOrDash(agent.certificate_expires_at, MAX_TIMESTAMP_LENGTH)}</td>
          <td>{boundedText(compatibility.get(agent.node_id) ?? "unknown", MAX_COMPATIBILITY_LENGTH)}</td>
          <td><Capabilities agent={agent}/></td>
          <td>{agent.agent_implementation === "python" && agent.migration_state === "required"
            ? <button
              type="button"
              disabled={grantPending || Boolean(grant)}
              onClick={() => void createMigrationGrant(agent.node_id)}
            >Create Rust migration grant</button>
            : "—"}</td>
        </tr>)}</tbody>
      </table></div>
      {agentPageCount > 1 && <div className="pagination">
        <button
          type="button"
          aria-label="Previous agent page"
          disabled={safeAgentPage === 0}
          onClick={() => setAgentPage(current => Math.max(0, current - 1))}
        >Previous agents</button>
        <button
          type="button"
          aria-label="Next agent page"
          disabled={safeAgentPage === agentPageCount - 1}
          onClick={() => setAgentPage(current => Math.min(agentPageCount - 1, current + 1))}
        >Next agents</button>
      </div>}
      {!loading && agents.length === 0 && <p>No enrolled agents.</p>}
      {visibleAgents
        .filter(agent => agent.state !== "revoked" && Boolean(agent.certificate_expires_at))
        .map(agent => <CertificateControls
          key={`${dataRevision}:${certificateSnapshot(agent)}`}
          agent={agent}
          disabled={loading}
          onRevoke={revoke}
        />)}
    </section>

    <section aria-labelledby="enrollment-heading">
      <h3 id="enrollment-heading">Enrollment evidence</h3>
      {enrollments.map(item => <EnrollmentReview
        key={`${dataRevision}:${enrollmentEvidenceSnapshot(item)}`}
        actionsDisabled={loading}
        enrollment={item}
        onApprove={approve}
        onReject={reject}
      />)}
      {!loading && enrollments.length === 0 && <p>No enrollment records.</p>}
    </section>
  </>;
}
