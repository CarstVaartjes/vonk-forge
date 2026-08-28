import {render, screen, waitFor, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import type {
  ControlApi,
  FleetProfile,
  FleetProfileApplication,
  FleetProfilePreview,
  VisualFleetNode,
} from "../api/types";
import {FleetOperatingBoard} from "./fleet-operating-board";

const NOW = new Date("2026-08-28T12:00:00Z");
const NODE_A = "spk_" + "1".repeat(32);
const NODE_B = "spk_" + "2".repeat(32);
const PROFILE_ID = "00000000-0000-4000-8000-000000000001";
const REVISION_ID = "00000000-0000-4000-8000-000000000002";

function node(id: string, name: string): VisualFleetNode {
  return {
    id,
    display_name: name,
    hostname: `${name.toLowerCase().replace(" ", "-")}.local`,
    lifecycle: "managed",
    labels: {},
    connection: {
      agent_state: "active",
      certificate_state: "valid",
      last_seen_age_seconds: 1,
      last_seen_at: NOW.toISOString(),
      offline_reason: null,
      online_state: "online",
    },
    installed: [],
    inventory: null,
    loaded: [],
    reservations: {disk_bytes: 0, gpu_memory_bytes: 0, host_memory_bytes: 0, port_count: 0, unified_memory_bytes: 0},
    telemetry: null,
    warnings: [],
  };
}

const profile: FleetProfile = {
  schema_version: 1,
  id: PROFILE_ID,
  name: "Studio Ready",
  description: "Keep DeepSeek ready across the studio pair.",
  installation_policy: "keep-cached",
  labels: {purpose: "interactive"},
  favorite: true,
  assignments: [{
    id: "00000000-0000-4000-8000-000000000003",
    recipe_id: "00000000-0000-4000-8000-000000000004",
    recipe_revision_id: REVISION_ID,
    recipe_title: "DeepSeek V4 Flash Mia dual Spark",
    model_title: "DeepSeek V4 Flash",
    topology_name: "dual",
    desired_state: "running",
    alias: "deepseek-studio",
    nodes: [
      {node_id: NODE_A, rank: 0, role: "leader", endpoint_owner: true},
      {node_id: NODE_B, rank: 1, role: "worker", endpoint_owner: false},
    ],
  }],
  profile_digest: "a".repeat(64),
  created_by: "admin",
  created_at: NOW.toISOString(),
  updated_at: NOW.toISOString(),
};

const preview: FleetProfilePreview = {
  schema_version: 1,
  profile_id: PROFILE_ID,
  profile_name: profile.name,
  profile_digest: profile.profile_digest,
  plan_digest: "b".repeat(64),
  generated_at: NOW.toISOString(),
  allowed: true,
  summary: {already_correct: 0, blockers: 0, distributions: 0, installs: 0, placements: 0, starts: 2, stops: 0, uninstalls: 0},
  assignments: [{assignment_id: profile.assignments[0].id, recipe_revision_id: REVISION_ID, recipe_title: profile.assignments[0].recipe_title, desired_state: "running", current_state: "installed", node_ids: [NODE_A, NODE_B], actions: ["start"], reasons: []}],
  reasons: [],
  steps: [
    {index: 0, kind: "start", assignment_id: profile.assignments[0].id, label: "Start DeepSeek on Spark Alpha", node_ids: [NODE_A]},
    {index: 1, kind: "start", assignment_id: profile.assignments[0].id, label: "Start DeepSeek on Spark Beta", node_ids: [NODE_B]},
  ],
};

const succeeded: FleetProfileApplication = {
  schema_version: 1,
  id: "00000000-0000-4000-8000-000000000005",
  profile_id: PROFILE_ID,
  profile_digest: profile.profile_digest,
  plan_digest: preview.plan_digest,
  state: "succeeded",
  current_operation_id: null,
  current_step: 2,
  total_steps: 2,
  progress: {completed_steps: 2},
  result: {changed: true},
  status_reason: null,
  created_at: NOW.toISOString(),
  updated_at: NOW.toISOString(),
};

test("maps live and desired workloads across Sparks and applies the exact preview", async () => {
  const alpha = node(NODE_A, "Spark Alpha");
  alpha.installed = [{
    installation_id: "installation-a",
    recipe_id: profile.assignments[0].recipe_id,
    recipe_revision_id: REVISION_ID,
    title: profile.assignments[0].recipe_title,
    topology_name: "dual",
    expected_rank_count: 2,
    present_ranks: [0, 1],
    member_node_ids: [NODE_A, NODE_B],
    rank: 0,
    role: "leader",
    rank_state: "installed",
    group_state: "installed",
    complete: true,
    degraded_reason: null,
  }];
  const beta = node(NODE_B, "Spark Beta");
  const applyFleetProfile = vi.fn(async () => succeeded);
  const api = {
    fleetProfiles: async () => ({schema_version: 1, generated_at: NOW.toISOString(), profiles: [profile]}),
    previewFleetProfile: async () => preview,
    applyFleetProfile,
  } as unknown as ControlApi;
  const manage = vi.fn();
  const user = userEvent.setup();

  render(<FleetOperatingBoard api={api} nodes={[alpha, beta]} now={NOW} onManageNode={manage}/>);

  expect(await screen.findByText("Studio Ready", {selector: ".fleet-profile-current strong"})).toBeVisible();
  const matrix = screen.getByRole("table");
  expect(within(matrix).getByText("DeepSeek V4 Flash")).toBeVisible();
  expect(within(matrix).getByText("Installed")).toBeVisible();
  expect(within(matrix).getByText("Profile change")).toBeVisible();
  const apply = await screen.findByRole("button", {name: "Apply 2 changes"});
  expect(screen.getByText("2 changes", {selector: ".profile-plan strong"})).toBeVisible();

  await user.click(apply);
  await waitFor(() => expect(applyFleetProfile).toHaveBeenCalledWith(PROFILE_ID, preview.plan_digest));
  expect(await screen.findByText("Profile applied", {selector: ".profile-application strong"})).toBeVisible();

  await user.click(screen.getByRole("button", {name: "Manage Spark Beta — stale"}));
  expect(manage).toHaveBeenCalledWith(NODE_B);
});

test("keeps a blocked profile readable and prevents apply", async () => {
  const blocked = {
    ...preview,
    allowed: false,
    summary: {...preview.summary, blockers: 1},
    reasons: [{code: "profile.build_missing", severity: "error" as const, detail: "Build the selected recipe before applying this profile."}],
    steps: [],
  };
  const api = {
    fleetProfiles: async () => ({schema_version: 1, generated_at: NOW.toISOString(), profiles: [profile]}),
    previewFleetProfile: async () => blocked,
  } as unknown as ControlApi;

  render(<FleetOperatingBoard api={api} nodes={[node(NODE_A, "Spark Alpha")]} now={NOW} onManageNode={() => undefined}/>);

  expect(await screen.findByText("1 blocked")).toBeVisible();
  expect(screen.getByText("Build the selected recipe before applying this profile.")).toBeVisible();
  expect(screen.getByRole("button", {name: "Resolve blockers"})).toBeDisabled();
});
