import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {
  ControlApi,
  UpdatePlan,
  UpdateRollout,
  UpdateSkew,
} from "../api/types";
import {App} from "../app";
import {FleetPage} from "./fleet";
import {UpdatesPage} from "./updates";

const nodeId = "spk_0123456789abcdef0123456789abcdef";
const planDigest = `sha256:${"c".repeat(64)}`;
const skewDigest = `sha256:${"d".repeat(64)}`;
const rolloutId = "11111111-1111-4111-8111-111111111111";
const targetSha = "7".repeat(64);

const target = {
  build_digest: `sha256:${"a".repeat(64)}`,
  platform_version: "2.0.0",
  protocol_maximum: 2,
  protocol_minimum: 1,
  release: `platform/releases/2.0.0/${targetSha}.json`,
  release_digest: `sha256:${targetSha}`,
  target_sha256: targetSha,
  tuf_targets_version: 7,
};

const skew: UpdateSkew = {
  affected_nodes: [nodeId],
  digest: skewDigest,
  incompatible_nodes: [],
  nodes: [{
    active_routes: ["chat"],
    active_slot: "A",
    active_workloads: ["model-a"],
    build_digest: `sha256:${"e".repeat(64)}`,
    compatible: true,
    display_name: "Alpha GPU node",
    node_id: nodeId,
    platform_version: "1.0.0",
    protocol_version: 1,
    reasons: ["control-release-newer"],
    rollback_slot: "B",
    status: "update-available",
    update_required: true,
  }],
  offline_pending: [],
  prompt_required: true,
  target,
};

const plan: UpdatePlan = {
  affected_routes: ["chat"],
  batches: [[nodeId]],
  canary_node: nodeId,
  gates: [{detail: "Protocol 1 is accepted", name: "agent protocol", status: "passed"}],
  incompatible: [],
  offline_pending: [],
  plan_digest: planDigest,
  rollback_slots: {[nodeId]: "B"},
  soak_seconds: 300,
  target,
  workloads: [{members: [nodeId], minimum_available: 0, workload_id: "model-a"}],
};

const rollout: UpdateRollout = {
  batches: [[nodeId]],
  can_approve_resume: false,
  current_batch: 0,
  failure_reason: null,
  id: rolloutId,
  job_id: "22222222-2222-4222-8222-222222222222",
  nodes: [{node_id: nodeId, state: "pending"}],
  plan_digest: planDigest,
  required_action: null,
  resume_required: false,
  state: "planned",
};

function api(overrides: Partial<ControlApi> = {}): ControlApi {
  return {
    updateSkew: async () => skew,
    planUpdate: async () => plan,
    applyUpdate: async () => rollout,
    updateStatus: async () => rollout,
    approveUpdateResume: async () => ({...rollout, can_approve_resume: false, resume_required: false, state: "updating"}),
    ...overrides,
  } as ControlApi;
}

afterEach(() => {
  history.replaceState(null, "", "/");
  localStorage.clear();
});

it("previews the server plan and applies only after exact digest confirmation", async () => {
  // Break caught: the browser locally derives a plan, sends a stale/partial
  // digest, or starts fan-out merely because the NAS is newer.
  const calls: [string, string][] = [];
  const control = api({
    planUpdate: async release => { calls.push(["plan", release]); return plan; },
    applyUpdate: async digest => { calls.push(["apply", digest]); return rollout; },
  });
  history.replaceState(null, "", "/updates");
  render(<UpdatesPage api={control}/>);
  const user = userEvent.setup();

  expect(await screen.findByText("2.0.0")).toBeVisible();
  expect(screen.getByText(targetSha)).toBeVisible();
  expect(screen.getByText("Targets metadata version 7")).toBeVisible();
  expect(screen.getByLabelText("Exact immutable TUF target")).toHaveValue(target.release);
  expect(screen.getByLabelText("Exact immutable TUF target")).toHaveAttribute("readonly");
  expect(calls).toEqual([]);
  await user.click(screen.getByRole("button", {name: "Preview signed update plan"}));

  expect(calls).toEqual([["plan", target.release]]);
  expect(await screen.findByText(planDigest)).toBeVisible();
  expect(screen.getByRole("button", {name: "Apply exact update plan"})).toBeDisabled();
  const review = screen.getByRole("region", {name: "Update plan review"});
  expect(review).toHaveTextContent(nodeId);
  expect(review).toHaveTextContent("Canary");
  expect(review).toHaveTextContent("model-a");
  expect(review).toHaveTextContent("chat");
  expect(review).toHaveTextContent("B");
  expect(review).toHaveTextContent("300 seconds");

  await user.type(screen.getByLabelText(/Type the exact plan digest/), `${planDigest}x`);
  expect(screen.getByRole("button", {name: "Apply exact update plan"})).toBeDisabled();
  await user.clear(screen.getByLabelText(/Type the exact plan digest/));
  await user.type(screen.getByLabelText(/Type the exact plan digest/), planDigest);
  await user.click(screen.getByRole("button", {name: "Apply exact update plan"}));

  expect(calls).toEqual([["plan", target.release], ["apply", planDigest]]);
  expect(await screen.findByRole("heading", {name: `Rollout ${rolloutId}`})).toBeVisible();
  expect(location.pathname).toBe("/updates");
  expect(new URLSearchParams(location.search).get("rollout")).toBe(rolloutId);
});

it("recovers a rollout from a canonical rollout or job UUID while skew is still loading", async () => {
  // Break caught: page reload loses the durable rollout because status lookup
  // waits for the independent skew request or rejects a job UUID alias.
  const jobId = rollout.job_id;
  history.replaceState(null, "", `/updates?rollout=${jobId}`);
  let resolveSkew!: (value: UpdateSkew) => void;
  const pendingSkew = new Promise<UpdateSkew>(resolve => { resolveSkew = resolve; });
  const lookups: string[] = [];
  const control = api({
    updateSkew: async () => pendingSkew,
    updateStatus: async id => { lookups.push(id); return rollout; },
  });

  render(<UpdatesPage api={control}/>);

  expect(await screen.findByRole("heading", {name: `Rollout ${rolloutId}`})).toBeVisible();
  expect(lookups).toEqual([jobId]);
  expect(screen.getByRole("status")).toHaveTextContent("Loading authoritative update state");
  expect(screen.queryByRole("region", {name: "Platform version skew"})).not.toBeInTheDocument();

  resolveSkew(skew);
  expect(await screen.findByRole("region", {name: "Platform version skew"})).toBeVisible();
});

it("does not query status for a non-canonical rollout UUID", async () => {
  history.replaceState(null, "", "/updates?rollout=AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA");
  const lookups: string[] = [];
  render(<UpdatesPage api={api({
    updateStatus: async id => { lookups.push(id); return rollout; },
  })}/>);

  expect(await screen.findByRole("region", {name: "Platform version skew"})).toBeVisible();
  expect(lookups).toEqual([]);
});

it("lists offline and incompatible nodes and fails closed on a stale apply", async () => {
  const offline = "spk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
  const incompatible = "spk_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
  const stalePlan = {...plan, incompatible: [incompatible], offline_pending: [offline]};
  const control = api({
    planUpdate: async () => stalePlan,
    applyUpdate: async () => { throw new Error("Control API returned 409: update plan digest is stale"); },
  });
  render(<UpdatesPage api={control}/>);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", {name: "Preview signed update plan"}));

  const review = await screen.findByRole("region", {name: "Update plan review"});
  expect(review).toHaveTextContent(offline);
  expect(review).toHaveTextContent(incompatible);
  expect(screen.getByRole("button", {name: "Apply exact update plan"})).toBeDisabled();
});

it("requires the exact destructive confirmation before rollback authorization, then labels resume separately", async () => {
  const rollbackRequired: UpdateRollout = {
    ...rollout,
    can_approve_resume: true,
    failure_reason: "canary readiness timed out",
    required_action: "authorize-rollback",
    resume_required: true,
    state: "waiting-for-approval",
  };
  let approvals = 0;
  const control = api({
    applyUpdate: async () => rollbackRequired,
    approveUpdateResume: async id => {
      expect(id).toBe(rolloutId);
      approvals += 1;
      if (approvals === 1) {
        return {
          ...rollbackRequired,
          failure_reason: "canary rolled back after readiness timeout",
          required_action: "approve-resume",
          state: "waiting-for-approval",
        };
      }
      return {
        ...rollbackRequired,
        can_approve_resume: false,
        required_action: null,
        resume_required: false,
        state: "updating",
      };
    },
  });
  render(<UpdatesPage api={control}/>);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", {name: "Preview signed update plan"}));
  await user.type(screen.getByLabelText(/Type the exact plan digest/), planDigest);
  await user.click(screen.getByRole("button", {name: "Apply exact update plan"}));

  const authorize = await screen.findByRole("button", {name: "Authorize GPU node rollback"});
  expect(authorize).toHaveClass("danger");
  expect(authorize).toBeDisabled();
  expect(screen.queryByRole("button", {name: "Approve rollout resume"})).not.toBeInTheDocument();

  await user.type(screen.getByLabelText(/Type ROLLBACK/), `ROLLBACK ${rolloutId}x`);
  expect(authorize).toBeDisabled();
  await user.clear(screen.getByLabelText(/Type ROLLBACK/));
  await user.type(screen.getByLabelText(/Type ROLLBACK/), `ROLLBACK ${rolloutId}`);
  await user.click(authorize);

  expect(approvals).toBe(1);
  expect(await screen.findByText(/canary rolled back/)).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Approve rollout resume"}));
  expect(approvals).toBe(2);
  expect(await screen.findByText(/updating/)).toBeVisible();

  render(<UpdatesPage api={api({applyUpdate: async () => ({...rollbackRequired, can_approve_resume: false, required_action: null})})}/>);
  expect(screen.queryByRole("button", {name: "Approve rollout resume"})).not.toBeInTheDocument();
});

it("keeps the NAS-newer fleet prompt persistent per exact skew digest without auto-apply", async () => {
  const fleet = {
    schema_version: 1 as const,
    event_cursor: 0,
    generated_at: "2026-08-15T12:00:00Z",
    repository_commit: "f".repeat(40),
    nodes: [],
  };
  let applies = 0;
  const control = api({
    visualFleet: async () => fleet,
    applyUpdate: async () => { applies += 1; return rollout; },
  });
  const first = render(<FleetPage api={control}/>);
  const user = userEvent.setup();

  const prompt = await screen.findByRole("region", {name: "GPU node update available"});
  expect(prompt).toHaveTextContent("2.0.0");
  expect(prompt).toHaveTextContent(target.build_digest);
  expect(prompt).toHaveTextContent("Alpha GPU node");
  expect(applies).toBe(0);
  await user.click(within(prompt).getByRole("button", {name: "Dismiss this exact update notice"}));
  expect(screen.queryByRole("region", {name: "GPU node update available"})).not.toBeInTheDocument();

  first.unmount();
  const second = render(<FleetPage api={control}/>);
  await waitFor(() => expect(control.updateSkew).toBeDefined());
  expect(screen.queryByRole("region", {name: "GPU node update available"})).not.toBeInTheDocument();
  second.unmount();

  render(<FleetPage api={api({visualFleet: async () => fleet, updateSkew: async () => ({...skew, digest: `sha256:${"e".repeat(64)}`})})}/>);
  expect(await screen.findByRole("region", {name: "GPU node update available"})).toBeVisible();
  expect(applies).toBe(0);
});

it("registers the updates page in primary navigation", async () => {
  history.replaceState(null, "", "/updates");
  render(<App api={api()}/>);

  expect(await screen.findByRole("heading", {name: "Platform updates"})).toBeVisible();
  expect(screen.getByRole("link", {name: "Updates"})).toHaveAttribute("aria-current", "page");
});
