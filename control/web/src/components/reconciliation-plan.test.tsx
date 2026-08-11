import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {components} from "../api/generated";
import type {ControlApi} from "../api/types";
import {ReconciliationPlan} from "./reconciliation-plan";

type Plan = components["schemas"]["ReconciliationPlanResponse"];
type Fleet = components["schemas"]["FleetStatusResponse"];

const nodeId = "spk_0123456789abcdef0123456789abcdef";
const dependencyId = "stop:model-a:spk_0123456789abcdef0123456789abcdef";
const digest = "d".repeat(64);
const evidenceDigest = "f".repeat(64);

const plan = {
  agent_protocol_range: [3, 4],
  commit: "a".repeat(40),
  digest,
  fleet_evidence_digest: evidenceDigest,
  input_digests: {fleet: "b".repeat(64), profile: "c".repeat(64)},
  operation_graph: {
    base_commit: "a".repeat(40),
    nodes: [
      {
        compensation_kind: "start",
        dependencies: [dependencyId],
        kind: "stop",
        node_id: nodeId,
        operation_id: "stop:model-b:spk_0123456789abcdef0123456789abcdef",
        payload_digest: "e".repeat(64),
        workload_id: "model-b",
      },
    ],
    schema_version: 1,
    targets: [nodeId],
  },
  placements: {"model-b": [nodeId]},
  reconciliation_id: "22222222-2222-4222-8222-222222222222",
  releases: {
    "model-b": {
      definition_hash: "1".repeat(64),
      endpoint: {path: "/v1", port: 8000, scheme: "https"},
      manifest_path: "manifests/releases/model-b.json",
      manifest_sha256: "2".repeat(64),
      release_request: {
        adapter_id: "systemd",
        oci_manifest_digest: `sha256:${"3".repeat(64)}`,
        provenance_digest: "4".repeat(64),
        schema_version: 1,
        target_digest: "5".repeat(64),
        target_name: "model-b",
      },
      workload_requests: {
        health: {adapter_id: "systemd", release_digest: "5".repeat(64), schema_version: 1, workload_id: "model-b"},
        prepare: {adapter_id: "systemd", profile_digest: "6".repeat(64), release_digest: "5".repeat(64), schema_version: 1, workload_id: "model-b"},
        start: {adapter_id: "systemd", preparation_digest: "7".repeat(64), release_digest: "5".repeat(64), schema_version: 1, workload_id: "model-b"},
        stop: {adapter_id: "systemd", release_digest: "5".repeat(64), schema_version: 1, workload_id: "model-b"},
        verify: {adapter_id: "systemd", expected_digest: "8".repeat(64), release_digest: "5".repeat(64), schema_version: 1, workload_id: "model-b"},
      },
    },
  },
  routes: {
    "chat": {
      entrypoint_node_id: nodeId,
      nodes: [nodeId],
      path: "/v1/chat/completions",
      port: 8000,
      quota: {requests_per_minute: 60, tokens_per_minute: 20_000},
      quota_digest: "9".repeat(64),
      scheme: "https",
      workload_id: "model-b",
    },
  },
  targets: [nodeId],
} as Plan;

const readyFleet = {
  commit: plan.commit,
  evidence_digest: evidenceDigest,
  nodes: [{
    agent_last_seen_at: "2026-08-05T12:00:00Z",
    agent_online: true,
    agent_state: "active",
    certificate_expires_at: "2026-09-01T00:00:00Z",
    certificate_expiry_seconds: 2_000_000,
    compatibility: "supported",
    disk_available_bytes: 1,
    display_name: "Compute A",
    healthy: true,
    hostname: "must-not-render.internal",
    id: nodeId,
    labels: {},
    last_seen_age_seconds: 2,
    last_seen_at: "2026-08-05T12:00:00Z",
    lifecycle: "managed",
    memory_available_bytes: 1,
    inventory_stale: false,
    probe_age_seconds: 2,
    profile: "profile-a",
    stale: false,
  }],
} as Fleet;

it("shows the canonical plan evidence and submits only its exact digest after typed confirmation", async () => {
  // Break caught: an operator cannot inspect a placement, stop/start dependency,
  // immutable release, route, compatibility gate, or can apply a different digest.
  const applied: [string, string][] = [];
  const api = {
    fleet: async () => readyFleet,
    applyReconciliation: async (planDigest: string, fleetEvidenceDigest: string) => {
      applied.push([planDigest, fleetEvidenceDigest]);
      return {base_commit: plan.commit, job_id: "job-1", reconciliation_id: plan.reconciliation_id, state: "queued"};
    },
  } as unknown as ControlApi;
  render(<ReconciliationPlan api={api} fleet={readyFleet} plan={plan}/>);
  const user = userEvent.setup();

  expect(screen.getByText(plan.commit)).toBeVisible();
  expect(screen.getAllByText(nodeId).length).toBeGreaterThan(0);
  expect(screen.getByText(dependencyId)).toBeVisible();
  expect(screen.getByText(`sha256:${"3".repeat(64)}`)).toBeVisible();
  expect(screen.getByText("https://Compute A:8000/v1/chat/completions")).toBeVisible();
  expect(screen.getByText(/Agent protocol 3–4/)).toBeVisible();
  expect(screen.getByText(/Ready and compatible/)).toBeVisible();
  expect(screen.queryByText("must-not-render.internal")).not.toBeInTheDocument();

  const apply = screen.getByRole("button", {name: "Apply exact plan"});
  expect(apply).toBeDisabled();
  await user.type(screen.getByLabelText(/Type the exact plan digest/), digest);
  expect(apply).toBeEnabled();
  await user.click(apply);

  expect(applied).toEqual([[digest, evidenceDigest]]);
  expect(await screen.findByRole("status")).toHaveTextContent("job-1");
});

it("fails closed when any target is unavailable and keeps the exact-digest action disabled", async () => {
  // Break caught: stale, offline, unhealthy, unknown, or incompatible target
  // state is presented as safe enough to mutate.
  const blockedFleet: Fleet = {
    ...readyFleet,
    nodes: readyFleet.nodes.map(node => ({
      ...node,
      agent_online: false,
      compatibility: "incompatible",
      healthy: false,
      stale: true,
    })),
  };
  render(<ReconciliationPlan api={{} as ControlApi} fleet={blockedFleet} plan={plan}/>);
  const user = userEvent.setup();

  const gate = screen.getByRole("row", {name: new RegExp(nodeId)});
  expect(within(gate).getByText(/Blocked/)).toBeVisible();
  expect(within(gate).getByText(/unavailable/)).toBeVisible();
  expect(within(gate).getByText(/stale/)).toBeVisible();
  expect(within(gate).getByText(/agent offline/)).toBeVisible();
  expect(within(gate).getByText(/incompatible/)).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent(/cannot be applied/);

  await user.type(screen.getByLabelText(/Type the exact plan digest/), digest);
  expect(screen.getByRole("button", {name: "Apply exact plan"})).toBeDisabled();
});

it("locks a rejected stale digest until the operator previews a new plan", async () => {
  // Break caught: a 409 leaves the old exact confirmation active and invites
  // repeated mutation attempts against authority the server rejected as stale.
  const api = {
    fleet: async () => readyFleet,
    applyReconciliation: async () => {
      throw new Error("Control API returned 409: reconciliation plan digest is stale");
    },
  } as unknown as ControlApi;
  render(<ReconciliationPlan api={api} fleet={readyFleet} plan={plan}/>);
  const user = userEvent.setup();

  await user.type(screen.getByLabelText(/Type the exact plan digest/), digest);
  const apply = screen.getByRole("button", {name: "Apply exact plan"});
  await user.click(apply);

  expect(await screen.findByRole("alert")).toHaveTextContent(/409/);
  expect(screen.getByRole("alert")).toHaveTextContent(/preview a new plan/i);
  expect(apply).toBeDisabled();
});

it.each([
  ["fleet commit mismatch with no targets", {...plan, targets: [], operation_graph: {...plan.operation_graph, targets: []}}, {...readyFleet, commit: "b".repeat(40)}],
  ["operation graph commit mismatch", {...plan, operation_graph: {...plan.operation_graph, base_commit: "b".repeat(40)}}, readyFleet],
  ["operation graph target omission", {...plan, operation_graph: {...plan.operation_graph, targets: []}}, readyFleet],
  ["duplicate authoritative target", {...plan, targets: [nodeId, nodeId], operation_graph: {...plan.operation_graph, targets: [nodeId, nodeId]}}, readyFleet],
  ["zero protocol lower bound", {...plan, agent_protocol_range: [0, 1]}, readyFleet],
  ["fractional protocol bound", {...plan, agent_protocol_range: [1.5, 2]}, readyFleet],
  ["reversed protocol range", {...plan, agent_protocol_range: [4, 3]}, readyFleet],
])("fails closed for %s", async (_name, candidate, fleet) => {
  // Break caught: malformed or mixed plan authority becomes applyable when
  // target-derived checks happen to be empty or otherwise look healthy.
  render(<ReconciliationPlan api={{} as ControlApi} fleet={fleet as Fleet} plan={candidate as Plan}/>);
  const user = userEvent.setup();

  await user.type(screen.getByLabelText(/Type the exact plan digest/), digest);

  expect(screen.getByRole("alert")).toHaveTextContent(/cannot be applied/i);
  expect(screen.getByRole("button", {name: "Apply exact plan"})).toBeDisabled();
});

it("refreshes and binds live evidence immediately before exact-digest apply", async () => {
  // Break caught: evidence changes after confirmation and the stale snapshot is
  // still submitted without a final authoritative refresh.
  const applied: unknown[] = [];
  const changedFleet = {...readyFleet, evidence_digest: "9".repeat(64)} as Fleet;
  const api = {
    fleet: async () => changedFleet,
    applyReconciliation: async (...args: unknown[]) => { applied.push(args); throw new Error("must not apply"); },
  } as unknown as ControlApi;
  render(<ReconciliationPlan api={api} fleet={readyFleet} plan={plan}/>);
  const user = userEvent.setup();

  await user.type(screen.getByLabelText(/Type the exact plan digest/), digest);
  await user.click(screen.getByRole("button", {name: "Apply exact plan"}));

  expect(await screen.findByRole("alert")).toHaveTextContent(/evidence changed/i);
  expect(applied).toEqual([]);
  expect(screen.getByRole("button", {name: "Apply exact plan"})).toBeDisabled();
});

it("fails closed when the pre-apply evidence refresh is unavailable", async () => {
  const api = {
    fleet: async () => { throw new Error("Control API returned 503"); },
    applyReconciliation: async () => { throw new Error("must not apply"); },
  } as unknown as ControlApi;
  render(<ReconciliationPlan api={api} fleet={readyFleet} plan={plan}/>);
  const user = userEvent.setup();

  await user.type(screen.getByLabelText(/Type the exact plan digest/), digest);
  await user.click(screen.getByRole("button", {name: "Apply exact plan"}));

  expect(await screen.findByRole("alert")).toHaveTextContent(/503/);
  expect(screen.getByRole("button", {name: "Apply exact plan"})).toBeDisabled();
});
