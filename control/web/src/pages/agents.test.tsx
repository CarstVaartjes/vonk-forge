import {fireEvent, render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {components} from "../api/generated";
import {ApiClient} from "../api/client";
import {App} from "../app";
import {AgentsPage} from "./agents";
import {FleetPage} from "./fleet";

type Agent = components["schemas"]["AgentSummary"];
type Enrollment = components["schemas"]["EnrollmentSummary"];
type Fleet = components["schemas"]["FleetStatusResponse"];

type Assert<T extends true> = T;
type SensitiveEnrollmentFields = Extract<
  keyof Enrollment,
  "address" | "private_key" | "certificate_body" | "certificate_chain" | "csr" | "grant_token"
>;
type EnrollmentContractIsSecretFree = Assert<SensitiveEnrollmentFields extends never ? true : false>;
const enrollmentContractIsSecretFree: EnrollmentContractIsSecretFree = true;

const nodeId = "spk_0123456789abcdef0123456789abcdef";
const enrollmentId = "enrollment-001";
const grantToken = "g".repeat(48);
const leakedListToken = "must-never-render-from-list";
const privateKey = "must-never-render-private-key";
const certificateBody = "must-never-render-certificate-body";

const agent: Agent = {
  agent_implementation: "python",
  capabilities: ["reconciliation", "telemetry"],
  certificate_expires_at: "2026-09-01T12:00:00Z",
  last_seen_age_seconds: 12,
  last_seen_at: "2026-08-05T09:59:48Z",
  node_id: nodeId,
  migration_state: "required",
  protocol_version: 4,
  stale: false,
  state: "active",
};

const enrollment: Enrollment = {
  agent_digest: "b".repeat(64),
  boot_id: "boot-001",
  certificate_fingerprint: null,
  certificate_serial: null,
  created_at: "2026-08-05T09:45:00Z",
  csr_public_key_fingerprint: "SHA256:csr-public-key",
  decided_at: null,
  decision_actor: null,
  hardware_fingerprint: "sha256:hardware-evidence",
  host_key_fingerprint: "SHA256:host-key-evidence",
  id: enrollmentId,
  node_id: nodeId,
  rejection_reason: null,
  state: "pending",
};

const fleet: Fleet = {
  commit: "a".repeat(40),
  evidence_digest: "e".repeat(64),
  nodes: [
    {
      agent_last_seen_at: "2026-08-05T09:59:48Z",
      agent_online: true,
      agent_state: "active",
      certificate_expires_at: "2026-09-01T12:00:00Z",
      certificate_expiry_seconds: 2_350_812,
      compatibility: "supported",
      disk_available_bytes: 2_000_000,
      display_name: "Alpha GPU node",
      healthy: true,
      hostname: "not-rendered.internal",
      id: nodeId,
      labels: {zone: "lab-a"},
      last_seen_age_seconds: 12,
      last_seen_at: "2026-08-05T09:59:48Z",
      lifecycle: "managed",
      memory_available_bytes: 1_000_000,
      inventory_stale: false,
      probe_age_seconds: 4,
      profile: "production",
      stale: false,
    },
  ],
};

type CapturedRequest = {body: unknown; method: string; path: string; signal: AbortSignal};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    headers: {"Content-Type": "application/json"},
    status,
  });
}

function installApiFake(options: {
  agents?: Agent[];
  enrollments?: Enrollment[];
  fleet?: Fleet;
  grantResponse?: Promise<Response>;
  refreshError?: Error;
  refreshGate?: Promise<void>;
} = {}) {
  const requests: CapturedRequest[] = [];
  const decisions = new Map<string, "approved" | "rejected">();
  const getCounts = new Map<string, number>();

  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(input, init);
    const url = new URL(request.url);
    const body = request.body ? await request.clone().json() : undefined;
    requests.push({body, method: request.method, path: url.pathname, signal: request.signal});

    if (request.method === "GET") {
      const previousCount = getCounts.get(url.pathname) ?? 0;
      getCounts.set(url.pathname, previousCount + 1);
      if (previousCount > 0) {
        await options.refreshGate;
        if (options.refreshError) throw options.refreshError;
      }
    }

    if (request.method === "GET" && url.pathname === "/api/v1/agents") {
      return jsonResponse({agents: options.agents ?? [agent]});
    }
    if (request.method === "GET" && url.pathname === "/api/v1/fleet") {
      return jsonResponse(options.fleet ?? fleet);
    }
    if (request.method === "GET" && url.pathname === "/api/v1/agents/enrollments") {
      return jsonResponse({
        enrollments: options.enrollments ?? [
          {
            ...enrollment,
            state: decisions.get(enrollmentId) ?? enrollment.state,
            address: "10.0.0.44",
            private_key: privateKey,
            certificate_body: certificateBody,
            grant_token: leakedListToken,
          },
        ],
        next_cursor: null,
      });
    }
    if (request.method === "POST" && url.pathname === "/api/v1/agents/enrollments/grants") {
      if (options.grantResponse) return options.grantResponse;
      return jsonResponse(
        {expires_at: "2026-08-05T10:15:00Z", id: "grant-001", node_id: nodeId, purpose: "new-node", token: grantToken},
        201,
      );
    }
    if (request.method === "POST" && url.pathname === `/api/v1/agents/nodes/${nodeId}/migration-grant`) {
      return jsonResponse(
        {expires_at: "2026-08-05T10:15:00Z", id: "grant-migration", node_id: nodeId, purpose: "rust-migration", token: grantToken},
        201,
      );
    }
    if (request.method === "POST" && url.pathname === `/api/v1/agents/enrollments/${enrollmentId}/approve`) {
      decisions.set(enrollmentId, "approved");
      return jsonResponse({id: enrollmentId, node_id: nodeId, state: "approved"});
    }
    if (request.method === "POST" && url.pathname === `/api/v1/agents/enrollments/${enrollmentId}/reject`) {
      decisions.set(enrollmentId, "rejected");
      return jsonResponse({id: enrollmentId, node_id: nodeId, state: "rejected"});
    }
    if (request.method === "POST" && url.pathname === `/api/v1/agents/nodes/${nodeId}/revoke`) {
      return new Response(null, {status: 204});
    }
    return jsonResponse({detail: "Unexpected test request"}, 404);
  });

  return requests;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(fulfill => { resolve = fulfill; });
  return {promise, resolve};
}

afterEach(() => {
  history.replaceState(null, "", "/");
  vi.unstubAllGlobals();
});

it("keeps the agents workflow reachable from the keyboard-operable primary navigation", async () => {
  // Break caught: shipping an isolated page without registering its route would strand the workflow.
  installApiFake();
  render(<App api={new ApiClient()}/>);
  const user = userEvent.setup();

  const agentsLink = screen.getByRole("link", {name: "Agents"});
  agentsLink.focus();
  await user.keyboard("{Enter}");

  expect(await screen.findByRole("heading", {name: "Agent enrollment and fleet"})).toBeVisible();
  expect(agentsLink).toHaveAttribute("aria-current", "page");
  expect(location.pathname).toBe("/agents");
});

it("keeps the current fleet page on the generated bounded node status contract", async () => {
  // Break caught: reducing FleetPage to its legacy DTO would hide agent/certificate compatibility state.
  installApiFake();
  render(<FleetPage api={new ApiClient()}/>);

  const table = await screen.findByRole("table", {name: "Vonk Forge GPU nodes"});
  const row = within(table).getByRole("row", {name: /Alpha GPU node/});
  expect(row).toHaveTextContent(nodeId);
  expect(row).toHaveTextContent("active");
  expect(row).toHaveTextContent("2026-08-05T09:59:48Z");
  expect(row).toHaveTextContent("2026-09-01T12:00:00Z");
  expect(row).toHaveTextContent("supported");
  expect(row).not.toHaveTextContent("not-rendered.internal");
});

it("keeps fleet and enrollment evidence semantic, bounded, and secret-free", async () => {
  // Break caught: rendering raw API payloads would disclose secret/address fields or omit typed fleet evidence.
  expect(enrollmentContractIsSecretFree).toBe(true);
  installApiFake();
  render(<AgentsPage api={new ApiClient()}/>);

  expect(await screen.findByRole("heading", {name: "Agent enrollment and fleet"})).toBeVisible();
  const agentTable = screen.getByRole("table", {name: "Enrolled agents"});
  expect(within(agentTable).getByRole("columnheader", {name: "Immutable node ID"})).toBeVisible();
  const agentRow = within(agentTable).getByRole("row", {name: new RegExp(nodeId)});
  expect(agentRow).toHaveTextContent("Protocol 4");
  expect(agentRow).toHaveTextContent("Python agent");
  expect(agentRow).toHaveTextContent("Migration required");
  expect(agentRow).toHaveTextContent("supported");
  expect(agentRow).toHaveTextContent("2026-08-05T09:59:48Z");
  expect(agentRow).toHaveTextContent("2026-09-01T12:00:00Z");

  const review = screen.getByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  expect(review).toHaveTextContent("SHA256:host-key-evidence");
  expect(review).toHaveTextContent("sha256:hardware-evidence");
  expect(review).toHaveTextContent("b".repeat(64));
  expect(review).toHaveTextContent("SHA256:csr-public-key");
  expect(review).toHaveTextContent("2026-08-05T09:45:00Z");
  expect(screen.queryByText(privateKey)).not.toBeInTheDocument();
  expect(screen.queryByText(certificateBody)).not.toBeInTheDocument();
  expect(screen.queryByText(leakedListToken)).not.toBeInTheDocument();
  expect(screen.queryByText("10.0.0.44")).not.toBeInTheDocument();

  const litellm = screen.getByRole("link", {name: /LiteLLM Admin UI.*keys, teams, and spend/i});
  const grafana = screen.getByRole("link", {name: /Grafana.*fleet dashboards/i});
  expect(new URL(litellm.getAttribute("href")!, location.origin).origin).toBe(location.origin);
  expect(new URL(litellm.getAttribute("href")!, location.origin).pathname).toBe("/litellm/ui/");
  expect(new URL(grafana.getAttribute("href")!, location.origin).origin).toBe(location.origin);
  expect(screen.getByText(/Local PostgreSQL remains recipe and routing authority/)).toBeVisible();
});

it("shows a grant token only for its creation response and clears it before reload", async () => {
  // Break caught: persisting a grant token in list-derived page state would redisplay a secret after refresh.
  const requests = installApiFake();
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  await screen.findByRole("table", {name: "Enrolled agents"});
  expect(screen.queryByText(grantToken)).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Grant node ID"), nodeId);
  const lifetime = screen.getByLabelText("Grant lifetime in seconds");
  expect(lifetime).toHaveAttribute("max", "600");
  await user.clear(lifetime);
  await user.type(lifetime, "300");
  await user.click(screen.getByRole("button", {name: "Create one-time grant"}));

  expect(await screen.findByRole("status", {name: "One-time enrollment grant"})).toHaveTextContent(grantToken);
  expect(screen.getAllByText(grantToken)).toHaveLength(1);
  expect(requests.find(request => request.path.endsWith("/grants"))).toEqual({
    body: {node_id: nodeId, ttl_seconds: 300},
    method: "POST",
    path: "/api/v1/agents/enrollments/grants",
    signal: expect.any(AbortSignal),
  });

  await user.click(screen.getByRole("button", {name: "Refresh agent data"}));
  expect(screen.queryByText(grantToken)).not.toBeInTheDocument();
  expect(screen.queryByText(leakedListToken)).not.toBeInTheDocument();
});

it("creates a dedicated Rust migration grant from a legacy agent row", async () => {
  const requests = installApiFake();
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  await screen.findByRole("table", {name: "Enrolled agents"});
  await user.click(screen.getByRole("button", {name: "Create Rust migration grant"}));

  const secret = await screen.findByRole("status", {name: "One-time enrollment grant"});
  expect(secret).toHaveTextContent("Rust migration");
  expect(secret).toHaveTextContent(grantToken);
  expect(requests.find(request => request.path.endsWith("/migration-grant"))).toEqual({
    body: {ttl_seconds: 300},
    method: "POST",
    path: `/api/v1/agents/nodes/${nodeId}/migration-grant`,
    signal: expect.any(AbortSignal),
  });
});

it("allows only one pending grant request and preserves its one display lifecycle", async () => {
  // Break caught: overlapping responses could replace a token before the administrator copied it.
  const pending = deferred<Response>();
  const requests = installApiFake({grantResponse: pending.promise});
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  await screen.findByRole("table", {name: "Enrolled agents"});
  await user.type(screen.getByLabelText("Grant node ID"), nodeId);
  const create = screen.getByRole("button", {name: "Create one-time grant"});
  await user.click(create);

  expect(create).toBeDisabled();
  expect(screen.getByRole("status", {name: "Enrollment grant request"})).toHaveTextContent(/creating/i);
  fireEvent.submit(create.closest("form")!);
  await waitFor(() => expect(
    requests.filter(request => request.path.endsWith("/grants")),
  ).toHaveLength(1));

  pending.resolve(jsonResponse(
    {expires_at: "2026-08-05T10:15:00Z", id: "grant-001", node_id: nodeId, purpose: "new-node", token: grantToken},
    201,
  ));
  expect(await screen.findByText(grantToken)).toBeVisible();
  expect(create).toBeDisabled();
  await user.click(screen.getByRole("button", {name: "Dismiss token"}));
  expect(screen.queryByText(grantToken)).not.toBeInTheDocument();
  expect(create).toBeEnabled();
});

it("aborts and invalidates a pending grant when refreshed", async () => {
  // Break caught: a late response after refresh could resurrect a one-time token from stale state.
  const pending = deferred<Response>();
  const requests = installApiFake({grantResponse: pending.promise});
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  await screen.findByRole("table", {name: "Enrolled agents"});
  await user.type(screen.getByLabelText("Grant node ID"), nodeId);
  await user.click(screen.getByRole("button", {name: "Create one-time grant"}));
  await waitFor(() => expect(
    requests.filter(request => request.path.endsWith("/grants")),
  ).toHaveLength(1));
  const grantRequest = requests.find(request => request.path.endsWith("/grants"))!;

  await user.click(screen.getByRole("button", {name: "Refresh agent data"}));
  expect(grantRequest.signal.aborted).toBe(true);
  pending.resolve(jsonResponse(
    {expires_at: "2026-08-05T10:15:00Z", id: "grant-001", node_id: nodeId, purpose: "new-node", token: grantToken},
    201,
  ));
  await waitFor(() => expect(screen.queryByText(grantToken)).not.toBeInTheDocument());
});

it("aborts a pending grant when the agents page unmounts", async () => {
  // Break caught: an unmounted page could leave a secret-bearing request active without a display owner.
  const pending = deferred<Response>();
  const requests = installApiFake({grantResponse: pending.promise});
  const view = render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  await screen.findByRole("table", {name: "Enrolled agents"});
  await user.type(screen.getByLabelText("Grant node ID"), nodeId);
  await user.click(screen.getByRole("button", {name: "Create one-time grant"}));
  await waitFor(() => expect(
    requests.filter(request => request.path.endsWith("/grants")),
  ).toHaveLength(1));
  const grantRequest = requests.find(request => request.path.endsWith("/grants"))!;

  view.unmount();

  expect(grantRequest.signal.aborted).toBe(true);
});

it("renders oversized agent and capability collections in reachable bounded chunks", async () => {
  // Break caught: mapping whole collections directly can create unbounded DOM/text output.
  const agents = Array.from({length: 45}, (_, index): Agent => ({
    ...agent,
    capabilities: Array.from(
      {length: 8},
      (_unused, capabilityIndex) => capabilityIndex === 0
        ? `cap-${index}-${"x".repeat(10_000)}`
        : `cap-${index}-${capabilityIndex}`,
    ),
    node_id: `spk_${index.toString(16).padStart(32, "0")}`,
  }));
  installApiFake({agents, enrollments: [], fleet: {...fleet, nodes: []}});
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  const table = await screen.findByRole("table", {name: "Enrolled agents"});
  expect(screen.getByRole("status", {name: "Agent result count"})).toHaveTextContent("Showing agents 1–20 of 45");
  expect(within(table).getAllByRole("row")).toHaveLength(21);
  expect(within(table).getByText(agents[0].node_id)).toBeVisible();
  expect(within(table).queryByText(agents[20].node_id)).not.toBeInTheDocument();
  expect(screen.queryByText(agents[0].capabilities[0])).not.toBeInTheDocument();
  expect(screen.getByRole("status", {name: `Capability result count for ${agents[0].node_id}`})).toHaveTextContent("Capabilities 1–3 of 8");

  await user.click(screen.getByRole("button", {name: `Next capabilities for ${agents[0].node_id}`}));
  expect(screen.getByRole("status", {name: `Capability result count for ${agents[0].node_id}`})).toHaveTextContent("Capabilities 4–6 of 8");
  await user.click(screen.getByRole("button", {name: "Next agent page"}));
  expect(screen.getByRole("status", {name: "Agent result count"})).toHaveTextContent("Showing agents 21–40 of 45");
  expect(within(table).getByText(agents[20].node_id)).toBeVisible();
  expect(within(table).queryByText(agents[0].node_id)).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", {name: "Next agent page"}));
  expect(screen.getByRole("status", {name: "Agent result count"})).toHaveTextContent("Showing agents 41–45 of 45");
  expect(within(table).getByText(agents[44].node_id)).toBeVisible();
  expect(screen.getAllByRole("region", {name: /Certificate controls for/})).toHaveLength(5);
});

it("bounds every generated string displayed in an agent summary row", async () => {
  // Break caught: a malicious summary response could mount multi-kilobyte state and timestamp strings.
  const oversizedState = `state-${"s".repeat(10_000)}`;
  const oversizedLastSeen = `last-seen-${"l".repeat(10_000)}`;
  const oversizedExpiry = `expiry-${"e".repeat(10_000)}`;
  const oversizedCompatibility = `compatibility-${"c".repeat(10_000)}`;
  installApiFake({
    agents: [{
      ...agent,
      certificate_expires_at: oversizedExpiry,
      last_seen_at: oversizedLastSeen,
      state: oversizedState,
    }],
    fleet: {
      ...fleet,
      nodes: [{...fleet.nodes[0], compatibility: oversizedCompatibility}],
    },
  });
  render(<AgentsPage api={new ApiClient()}/>);

  const table = await screen.findByRole("table", {name: "Enrolled agents"});
  const row = within(table).getByRole("row", {name: new RegExp(nodeId)});
  const cells = within(row).getAllByRole("cell");

  expect(cells[0]).toHaveTextContent("state-");
  expect(cells[1]).toHaveTextContent("last-seen-");
  expect(cells[2]).toHaveTextContent("expiry-");
  expect(cells[3]).toHaveTextContent("compatibility-");
  for (const cell of cells.slice(0, 4)) {
    expect(cell).toHaveTextContent("…");
    expect(cell.textContent!.length).toBeLessThan(128);
  }
  expect(screen.queryByText(oversizedState)).not.toBeInTheDocument();
  expect(screen.queryByText(oversizedLastSeen)).not.toBeInTheDocument();
  expect(screen.queryByText(oversizedExpiry)).not.toBeInTheDocument();
  expect(screen.queryByText(oversizedCompatibility)).not.toBeInTheDocument();
});

it("requires keyboard-operable evidence confirmation before approval", async () => {
  // Break caught: an enabled approval action could authorize an agent before evidence is compared.
  const requests = installApiFake();
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  const review = await screen.findByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  const approve = within(review).getByRole("button", {name: "Approve enrollment"});
  expect(approve).toBeDisabled();
  const confirmation = within(review).getByRole("checkbox", {name: /compared all fingerprints/i});
  confirmation.focus();
  await user.keyboard(" ");
  expect(confirmation).toBeChecked();
  expect(approve).toBeEnabled();
  approve.focus();
  await user.keyboard("{Enter}");

  expect(await screen.findByText(`Enrollment for ${nodeId} approved`)).toBeVisible();
  expect(requests.some(request => request.method === "POST" && request.path.endsWith("/approve"))).toBe(true);
});

it("clears destructive confirmations when refreshed evidence or certificate state is loaded", async () => {
  // Break caught: confirmations from one reviewed snapshot could authorize a later, different snapshot.
  const refreshedFingerprint = "SHA256:refreshed-host-key-evidence";
  const enrollments = [{...enrollment}];
  installApiFake({agents: [{...agent}], enrollments});
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  const review = await screen.findByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  const approval = within(review).getByRole("checkbox", {name: /compared all fingerprints/i});
  const reason = within(review).getByLabelText("Rejection reason");
  const rejectionConfirmation = within(review).getByLabelText(`Type ${nodeId} to confirm rejection`);
  await user.click(approval);
  await user.type(reason, "Stale reason");
  await user.type(rejectionConfirmation, nodeId);

  const certificate = screen.getByRole("region", {name: `Certificate controls for ${nodeId}`});
  const revocationConfirmation = within(certificate).getByLabelText(
    `Type ${nodeId} to confirm certificate revocation`,
  );
  await user.type(revocationConfirmation, nodeId);
  enrollments[0] = {...enrollments[0], host_key_fingerprint: refreshedFingerprint};

  await user.click(screen.getByRole("button", {name: "Refresh agent data"}));

  expect(await screen.findByText(refreshedFingerprint)).toBeVisible();
  const refreshedReview = screen.getByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  expect(within(refreshedReview).getByRole("checkbox", {name: /compared all fingerprints/i})).not.toBeChecked();
  expect(within(refreshedReview).getByLabelText("Rejection reason")).toHaveValue("");
  expect(within(refreshedReview).getByLabelText(`Type ${nodeId} to confirm rejection`)).toHaveValue("");
  const refreshedCertificate = screen.getByRole("region", {name: `Certificate controls for ${nodeId}`});
  expect(within(refreshedCertificate).getByLabelText(
    `Type ${nodeId} to confirm certificate revocation`,
  )).toHaveValue("");
});

it("invalidates and disables confirmations while refresh is pending", async () => {
  // Break caught: a slow reload could leave confirmations actionable against the pre-refresh snapshot.
  const refresh = deferred<void>();
  installApiFake({refreshGate: refresh.promise});
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  const review = await screen.findByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  await user.click(within(review).getByRole("checkbox", {name: /compared all fingerprints/i}));
  await user.type(within(review).getByLabelText("Rejection reason"), "Stale reason");
  await user.type(within(review).getByLabelText(`Type ${nodeId} to confirm rejection`), nodeId);
  const certificate = screen.getByRole("region", {name: `Certificate controls for ${nodeId}`});
  await user.type(within(certificate).getByLabelText(
    `Type ${nodeId} to confirm certificate revocation`,
  ), nodeId);

  await user.click(screen.getByRole("button", {name: "Refresh agent data"}));

  expect(screen.getByText("Loading agent data…")).toBeVisible();
  const pendingReview = screen.getByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  expect(within(pendingReview).getByRole("checkbox", {name: /compared all fingerprints/i})).not.toBeChecked();
  expect(within(pendingReview).getByRole("checkbox", {name: /compared all fingerprints/i})).toBeDisabled();
  expect(within(pendingReview).getByLabelText("Rejection reason")).toHaveValue("");
  expect(within(pendingReview).getByRole("button", {name: "Reject enrollment"})).toBeDisabled();
  const pendingCertificate = screen.getByRole("region", {name: `Certificate controls for ${nodeId}`});
  expect(within(pendingCertificate).getByLabelText(
    `Type ${nodeId} to confirm certificate revocation`,
  )).toHaveValue("");
  expect(within(pendingCertificate).getByRole("button", {name: "Revoke node certificate"})).toBeDisabled();
});

it("does not restore confirmations after a failed refresh", async () => {
  // Break caught: a failed reload could permanently retain actionable pre-refresh confirmations.
  installApiFake({refreshError: new Error("Refresh unavailable")});
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  const review = await screen.findByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  await user.click(within(review).getByRole("checkbox", {name: /compared all fingerprints/i}));
  await user.type(within(review).getByLabelText("Rejection reason"), "Stale reason");
  await user.type(within(review).getByLabelText(`Type ${nodeId} to confirm rejection`), nodeId);
  const certificate = screen.getByRole("region", {name: `Certificate controls for ${nodeId}`});
  await user.type(within(certificate).getByLabelText(
    `Type ${nodeId} to confirm certificate revocation`,
  ), nodeId);

  await user.click(screen.getByRole("button", {name: "Refresh agent data"}));

  expect(await screen.findByText("Refresh unavailable")).toBeVisible();
  const failedReview = screen.getByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  expect(within(failedReview).getByRole("checkbox", {name: /compared all fingerprints/i})).not.toBeChecked();
  expect(within(failedReview).getByLabelText("Rejection reason")).toHaveValue("");
  expect(within(failedReview).getByLabelText(`Type ${nodeId} to confirm rejection`)).toHaveValue("");
  expect(within(failedReview).getByRole("button", {name: "Approve enrollment"})).toBeDisabled();
  expect(within(failedReview).getByRole("button", {name: "Reject enrollment"})).toBeDisabled();
  const failedCertificate = screen.getByRole("region", {name: `Certificate controls for ${nodeId}`});
  expect(within(failedCertificate).getByLabelText(
    `Type ${nodeId} to confirm certificate revocation`,
  )).toHaveValue("");
  expect(within(failedCertificate).getByRole("button", {name: "Revoke node certificate"})).toBeDisabled();
});

it("requires exact typed administrator confirmation for rejection and certificate revocation", async () => {
  // Break caught: destructive controls could act on the wrong node without an irreversible warning and exact ID check.
  const requests = installApiFake();
  render(<AgentsPage api={new ApiClient()}/>);
  const user = userEvent.setup();

  const review = await screen.findByRole("region", {name: `Enrollment evidence for ${nodeId}`});
  expect(within(review).getByRole("alert")).toHaveTextContent(/cannot be undone/i);
  const reject = within(review).getByRole("button", {name: "Reject enrollment"});
  await user.type(within(review).getByLabelText("Rejection reason"), "Inventory evidence does not match");
  await user.type(within(review).getByLabelText(`Type ${nodeId} to confirm rejection`), nodeId.slice(0, -1));
  expect(reject).toBeDisabled();
  await user.type(within(review).getByLabelText(`Type ${nodeId} to confirm rejection`), nodeId.slice(-1));
  expect(reject).toBeEnabled();
  await user.click(reject);
  expect(await screen.findByText(`Enrollment for ${nodeId} rejected`)).toBeVisible();

  const revokeRegion = screen.getByRole("region", {name: `Certificate controls for ${nodeId}`});
  expect(within(revokeRegion).getByRole("alert")).toHaveTextContent(/immediately disconnects.*cannot be undone/i);
  const revoke = within(revokeRegion).getByRole("button", {name: "Revoke node certificate"});
  await user.type(within(revokeRegion).getByLabelText(`Type ${nodeId} to confirm certificate revocation`), nodeId);
  expect(revoke).toBeEnabled();
  await user.click(revoke);
  expect(await screen.findByText(`Certificate for ${nodeId} revoked`)).toBeVisible();
  expect(screen.queryByRole("region", {name: `Certificate controls for ${nodeId}`})).not.toBeInTheDocument();

  expect(requests.find(request => request.path.endsWith("/reject"))?.body).toEqual({
    reason: "Inventory evidence does not match",
  });
  expect(requests.some(request => request.method === "POST" && request.path.endsWith("/revoke"))).toBe(true);
});
