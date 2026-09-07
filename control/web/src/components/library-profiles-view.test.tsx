import {render, screen, within} from "@testing-library/react";
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
  progress: {
    completed_steps: 0,
    total_steps: 3,
    child_progress: {
      phase: "target-copy",
      bytes: 4 * 1024 ** 3,
      total_bytes: 8 * 1024 ** 3,
      node_ids: [nodeA, nodeB],
    },
  },
  result: null,
  created_at: "2026-09-05T00:00:00Z",
  updated_at: "2026-09-05T00:00:00Z",
} satisfies FleetProfileApplication;

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

  expect(await screen.findByText("Sparks in scope")).toBeVisible();
  expect(await screen.findByText(/1 idle by choice · 2 Sparks in scope/)).toBeVisible();
  const switchButton = await screen.findByRole("button", {name: "Switch profile"});
  await user.click(switchButton);

  expect(applyFleetProfile).toHaveBeenCalledTimes(1);
  expect(applyFleetProfile).toHaveBeenCalledWith(profileId, {plan_digest: preview.plan_digest, request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)});
  expect(screen.queryByRole("button", {name: "Review switch effects"})).not.toBeInTheDocument();
  expect(screen.getByText("Copying to Spark A, Spark B")).toBeVisible();
  const progress = screen.getByRole("region", {name: "Profile switch progress"});
  expect(progress).toHaveTextContent("4.0 GiB of 8.0 GiB");
  expect(within(progress).getByRole("progressbar", {name: "Profile switch progress"})).toHaveAttribute("aria-valuenow", "50");
  expect(within(progress).getByRole("list", {name: "Profile switch targets"})).toHaveTextContent("Spark A");
  expect(within(progress).getByRole("list", {name: "Profile switch targets"})).toHaveTextContent("Spark B");
});

test("holds cleanup behind an explicit confirmation while keeping normal switches one click", async () => {
  const user = userEvent.setup();
  const fleetProfiles = vi.fn(async () => ({schema_version: 2, generated_at: "2026-09-05T00:00:00Z", profiles: [profile]}));
  const fleetProfileStatus = vi.fn(async () => ({schema_version: 2, profile_id: profileId, profile_digest: digest, state: "drifted", matched: false, drifted: true, scope: {node_ids: [nodeA, nodeB], idle_node_ids: [nodeA]}, reasons: [], generated_at: "2026-09-05T00:00:00Z"}));
  const cleanupPreview = {...preview, summary: {...preview.summary, uninstalls: 1}, reasons: [{code: "profile.cleanup_required", detail: "The exact retention policy would remove an unlisted installation.", severity: "warning"}]} as unknown as FleetProfilePreview;
  const previewFleetProfile = vi.fn(async () => cleanupPreview);
  const applyFleetProfile = vi.fn(async () => application);
  const api = {fleetProfiles, fleetProfileStatus, previewFleetProfile, applyFleetProfile} as unknown as ControlApi;

  render(<LibraryProfilesView api={api} entries={[]} fleet={fleet} onNavigate={vi.fn()} />);

  const reviewButton = await screen.findByRole("button", {name: "Review cleanup"});
  await user.click(reviewButton);
  expect(applyFleetProfile).not.toHaveBeenCalled();
  const confirmation = screen.getByRole("alert");
  expect(confirmation).toHaveTextContent("Cleanup requires confirmation");
  await user.click(within(confirmation).getByRole("button", {name: "Confirm switch"}));
  expect(applyFleetProfile).toHaveBeenCalledTimes(1);
});

function editingApi() {
  return {
    fleetProfiles: vi.fn(async () => ({profiles: [profile]})),
    fleetProfileStatus: vi.fn(async () => ({state: "drifted", scope: {node_ids: [nodeA, nodeB]}, reasons: []})),
    previewFleetProfile: vi.fn(async () => preview),
    updateFleetProfile: vi.fn(async (_id: string, input: Record<string, unknown>) => ({...profile, ...input})),
    createFleetProfile: vi.fn(async (input: Record<string, unknown>) => ({...profile, ...input})),
  };
}

test("blocks an empty scope and lets an operator restore scope and save", async () => {
  const user = userEvent.setup();
  const api = editingApi();
  render(<LibraryProfilesView api={api as unknown as ControlApi} entries={[]} fleet={fleet} onNavigate={vi.fn()} />);
  await user.click(await screen.findByRole("button", {name: "Edit profile"}));
  await user.click(screen.getByRole("button", {name: "Clear scope"}));
  const scope = screen.getByRole("group", {name: "Fleet scope"});
  expect(scope).toHaveAttribute("aria-invalid", "true");
  expect(scope).toHaveAccessibleDescription(/Select at least one Spark/);
  const save = screen.getByRole("button", {name: "Save profile"});
  expect(save).toBeDisabled();
  await user.click(save);
  expect(api.updateFleetProfile).not.toHaveBeenCalled();
  await user.click(within(scope).getByRole("checkbox", {name: /Spark B/}));
  expect(scope).toHaveAttribute("aria-invalid", "false");
  expect(screen.queryByText(/Select at least one Spark/)).not.toBeInTheDocument();
  await user.click(save);
  expect(api.updateFleetProfile).toHaveBeenCalledWith(profileId, expect.objectContaining({scope: {node_ids: [nodeB]}}));
});

test("blocks empty placement ranks and preserves fields while ranks are repaired", async () => {
  const user = userEvent.setup();
  const api = editingApi();
  render(<LibraryProfilesView api={api as unknown as ControlApi} entries={[]} fleet={fleet} onNavigate={vi.fn()} />);
  await user.click(await screen.findByRole("button", {name: "Edit profile"}));
  const ranks = screen.getByRole("group", {name: "Spark ranks"});
  await user.click(within(ranks).getByRole("checkbox", {name: "Spark B"}));
  expect(ranks).toHaveAttribute("aria-invalid", "true");
  expect(ranks).toHaveAccessibleDescription(/Select the Sparks for this placement before saving/);
  expect(screen.getByLabelText("Endpoint alias")).toHaveValue("solo");
  const save = screen.getByRole("button", {name: "Save profile"});
  expect(save).toBeDisabled();
  await user.click(save);
  expect(api.updateFleetProfile).not.toHaveBeenCalled();
  await user.click(within(ranks).getByRole("checkbox", {name: "Spark A"}));
  expect(ranks).toHaveAttribute("aria-invalid", "false");
  await user.click(save);
  expect(api.updateFleetProfile).toHaveBeenCalledWith(profileId, expect.objectContaining({assignments: [expect.objectContaining({alias: "solo", nodes: [{node_id: nodeA, rank: 0, role: "leader", endpoint_owner: true}]})]}));
});

test("saves an intentional all-idle profile by removing its last placement", async () => {
  const user = userEvent.setup();
  const api = editingApi();
  render(<LibraryProfilesView api={api as unknown as ControlApi} entries={[]} fleet={fleet} onNavigate={vi.fn()} />);
  await user.click(await screen.findByRole("button", {name: "Edit profile"}));
  await user.click(screen.getByRole("button", {name: "Remove placement"}));
  expect(screen.getByText("All scoped Sparks are idle")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Save profile"}));
  expect(api.updateFleetProfile).toHaveBeenCalledWith(profileId, expect.objectContaining({scope: {node_ids: [nodeA, nodeB]}, assignments: []}));
});

test("does not offer to save a new profile before a Spark is enrolled", async () => {
  const api = editingApi();
  render(<LibraryProfilesView api={api as unknown as ControlApi} entries={[]} fleet={{...fleet, nodes: []}} initialCreate onNavigate={vi.fn()} />);
  expect(await screen.findByText("No Sparks are enrolled. Enroll a Spark before saving a profile.")).toBeVisible();
  expect(screen.getAllByRole("button", {name: "Create profile"}).at(-1)).toBeDisabled();
  expect(api.createFleetProfile).not.toHaveBeenCalled();
});

test("retries remaining work once and follows the returned application", async () => {
  const user = userEvent.setup();
  const failed = {...application, state: "failed", status_reason: "Spark B disconnected"};
  const retried = {...application, id: "66666666-6666-4666-8666-666666666666"};
  let resolveRetry!: (value: FleetProfileApplication) => void;
  const retryFleetProfileApplication = vi.fn(() => new Promise<FleetProfileApplication>(resolve => { resolveRetry = resolve; }));
  const fleetProfileApplication = vi.fn(async () => ({...retried, state: "succeeded"}));
  const api = {...editingApi(), applyFleetProfile: vi.fn(async () => failed), retryFleetProfileApplication, fleetProfileApplication};
  render(<LibraryProfilesView api={api as unknown as ControlApi} entries={[]} fleet={fleet} onNavigate={vi.fn()} />);
  await user.click(await screen.findByRole("button", {name: "Switch profile"}));
  await user.click(await screen.findByRole("button", {name: "Retry remaining work"}));
  expect(screen.getByRole("button", {name: "Retrying remaining work…"})).toBeDisabled();
  expect(retryFleetProfileApplication).toHaveBeenCalledExactlyOnceWith(application.id, {request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)});
  resolveRetry(retried);
  expect(await screen.findByText("Remaining profile work is being rechecked against the current fleet.")).toBeVisible();
  await screen.findByText("succeeded", {}, {timeout: 2500});
  expect(fleetProfileApplication).toHaveBeenCalledWith(retried.id, expect.any(AbortSignal));
});

test("keeps failed application evidence and reuses the request key after an uncertain retry", async () => {
  const user = userEvent.setup();
  const retryFleetProfileApplication = vi.fn().mockRejectedValue(new Error("Connection interrupted"));
  const api = {...editingApi(), applyFleetProfile: vi.fn(async () => ({...application, state: "failed", status_reason: "Spark B disconnected"})), retryFleetProfileApplication};
  render(<LibraryProfilesView api={api as unknown as ControlApi} entries={[]} fleet={fleet} onNavigate={vi.fn()} />);
  await user.click(await screen.findByRole("button", {name: "Switch profile"}));
  await user.click(await screen.findByRole("button", {name: "Retry remaining work"}));
  expect(await screen.findByRole("alert")).toHaveTextContent("Connection interrupted");
  expect(screen.getByText("Spark B disconnected")).toBeVisible();
  await user.click(screen.getByRole("button", {name: "Retry remaining work"}));
  expect(retryFleetProfileApplication).toHaveBeenCalledTimes(2);
  expect(retryFleetProfileApplication.mock.calls[1]).toEqual(retryFleetProfileApplication.mock.calls[0]);
});
