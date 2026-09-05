import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {ControlApi, FleetProfile, FleetProfileApplication, FleetProfilePreview, VisualFleetSnapshot} from "../api/types";
import {LibraryProfilesView} from "./library-profiles-view";

const profileId = "11111111-1111-4111-8111-111111111111";
const nodeA = "spk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const nodeB = "spk_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const digest = "a".repeat(64);

const profile = {
  schema_version: 2,
  id: profileId,
  name: "Solo on B",
  description: "Run the solo service on Spark B and leave Spark A idle.",
  installation_policy: "keep-cached",
  labels: {purpose: "test"},
  favorite: true,
  profile_digest: digest,
  created_by: "admin",
  created_at: "2026-09-05T00:00:00Z",
  updated_at: "2026-09-05T00:00:00Z",
  scope: {node_ids: [nodeA, nodeB]},
  assignments: [{
    id: "22222222-2222-4222-8222-222222222222",
    recipe_id: "recipe-solo",
    recipe_revision_id: "33333333-3333-4333-8333-333333333333",
    recipe_title: "Solo service",
    model_title: "Qwen 3",
    topology_name: "solo",
    desired_state: "running",
    alias: "solo",
    nodes: [{node_id: nodeB, rank: 0, role: "leader", endpoint_owner: true}],
  }],
} as unknown as FleetProfile;

const preview = {
  schema_version: 2,
  profile_id: profileId,
  profile_name: profile.name,
  profile_digest: digest,
  generated_at: "2026-09-05T00:00:00Z",
  allowed: true,
  scope: {node_ids: [nodeA, nodeB], idle_node_ids: [nodeA]},
  assignments: [{
    assignment_id: profile.assignments[0]!.id,
    recipe_revision_id: profile.assignments[0]!.recipe_revision_id,
    recipe_title: "Solo service",
    desired_state: "running",
    current_state: "running",
    node_ids: [nodeB],
    actions: ["switch"],
    reasons: [],
  }],
  steps: [
    {index: 0, kind: "stop", owner_id: "44444444-4444-4444-8444-444444444444", node_ids: [nodeA, nodeB], label: "Stop the previous dual run"},
    {index: 1, kind: "switch", node_ids: [nodeA, nodeB], label: "Switch the selected profile"},
    {index: 2, kind: "start", assignment_id: profile.assignments[0]!.id, node_ids: [nodeB], label: "Start Solo service"},
  ],
  summary: {already_correct: 0, placements: 0, builds: 0, distributions: 0, installs: 0, starts: 1, stops: 1, uninstalls: 0, blockers: 0},
  reasons: [{code: "profile.interruption_expected", detail: "Expected in-scope replacement: Spark A becomes idle while the solo run starts on Spark B.", severity: "warning"}],
  plan_digest: "b".repeat(64),
} as unknown as FleetProfilePreview;

const application = {
  schema_version: 2,
  id: "55555555-5555-4555-8555-555555555555",
  profile_id: profileId,
  profile_digest: digest,
  plan_digest: preview.plan_digest,
  state: "queued",
  current_step: 0,
  total_steps: 3,
  current_operation_id: null,
  status_reason: null,
  progress: {phase: "switch"},
  result: null,
  created_at: "2026-09-05T00:00:00Z",
  updated_at: "2026-09-05T00:00:00Z",
} as unknown as FleetProfileApplication;

const fleet = {
  schema_version: 1,
  generated_at: "2026-09-05T00:00:00Z",
  nodes: [
    {id: nodeA, display_name: "Spark A", hostname: "spark-a", loaded: [], installed: []},
    {id: nodeB, display_name: "Spark B", hostname: "spark-b", loaded: [], installed: []},
  ],
} as unknown as VisualFleetSnapshot;

test("switches an in-scope dual-to-solo replacement on the first click and keeps the idle Spark explicit", async () => {
  const user = userEvent.setup();
  const fleetProfiles = vi.fn(async () => ({schema_version: 2, generated_at: "2026-09-05T00:00:00Z", profiles: [profile]}));
  const fleetProfileStatus = vi.fn(async () => ({schema_version: 2, profile_id: profileId, profile_digest: digest, state: "drifted", matched: false, drifted: true, scope: {node_ids: [nodeA, nodeB], idle_node_ids: [nodeA]}, reasons: [], generated_at: "2026-09-05T00:00:00Z"}));
  const previewFleetProfile = vi.fn(async () => preview);
  const applyFleetProfile = vi.fn(async () => application);
  const api = {fleetProfiles, fleetProfileStatus, previewFleetProfile, applyFleetProfile} as unknown as ControlApi;

  render(<LibraryProfilesView api={api} entries={[]} fleet={fleet} onNavigate={vi.fn()} />);

  expect(await screen.findByText("2 of 2 selected")).toBeVisible();
  expect(await screen.findByText(/Expected in-scope replacement/)).toBeInTheDocument();
  expect(screen.getByText("1 intentional idle · 2 Sparks in scope")).toBeVisible();
  const switchButton = await screen.findByRole("button", {name: "Switch profile"});
  await user.click(switchButton);

  expect(applyFleetProfile).toHaveBeenCalledTimes(1);
  expect(applyFleetProfile).toHaveBeenCalledWith(profileId, {plan_digest: preview.plan_digest, request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)});
  expect(screen.queryByRole("button", {name: "Review switch effects"})).not.toBeInTheDocument();
  expect(screen.getByText("switch")).toBeVisible();
});
