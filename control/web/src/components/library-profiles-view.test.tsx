import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import type {ControlApi, FleetProfile, FleetProfileApplicationView, FleetProfilePreview} from "../api/types";
import {LibraryProfilesView} from "./library-profiles-view";

const nodeA = "spk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const nodeB = "spk_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const profile = {
  schema_version: 2, id: "11111111-1111-4111-8111-111111111111", number: 2, revision: 4,
  name: "Coding", description: "Code on Spark A", installation_policy: "keep-cached", labels: {}, favorite: true,
  assignments: [{selector: "qwen-code", display_name: "Qwen Code", recipe_selector: "qwen-code", recipe_id: null, spark_ids: [nodeA], required_sparks: 1, assigned_sparks: 1, model: {variant: "nvfp4", state: "cached"}, recipe: {selector: "qwen-code", name: "Qwen Code", state: "cached", revision_id: "33333333-3333-4333-8333-333333333333"}, resources: {}, observed_state: "Not loaded"}],
  fleet: [{selector: nodeA, display_name: "Spark A", state: "idle"}, {selector: nodeB, display_name: "Spark B", state: "idle"}], status: "ready", loaded_revision: null, cache_summary: {}, warnings: [], next_actions: [], profile_digest: "a".repeat(64), created_by: "admin", created_at: "2026-09-10T00:00:00Z", updated_at: "2026-09-10T00:00:00Z",
} as unknown as FleetProfile;
const preview = {allowed: true, steps: [{index: 0, kind: "start", label: "Start Qwen Code", node_ids: [nodeA]}], reasons: []} as unknown as FleetProfilePreview;
const application = {state: "running", progress: {child_progress: {phase: "start", node_ids: [nodeA], bytes: 50, total_bytes: 100}}, status_reason: null} as unknown as FleetProfileApplicationView;

function apiFor(overrides: Partial<ControlApi> = {}): ControlApi {
  return {profiles: vi.fn(async () => ({schema_version: 2 as const, generated_at: "2026-09-10T00:00:00Z", profiles: [profile]})), previewProfile: vi.fn(async () => preview), autosaveProfile: vi.fn(async () => profile), loadProfile: vi.fn(async () => application), profileProgress: vi.fn(async () => application), ...overrides} as unknown as ControlApi;
}

test("reads and loads a numbered profile without legacy status or application routes", async () => {
  const user = userEvent.setup();
  const api = apiFor();
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  expect((await screen.findAllByText("Profile 2 · Coding"))[0]).toBeVisible();
  await user.click(await screen.findByRole("button", {name: "Load profile"}));
  expect(api.previewProfile).toHaveBeenCalledWith(2, expect.any(AbortSignal));
  expect(api.loadProfile).toHaveBeenCalledWith(2, {dry_run: false, request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)});
  expect(await screen.findByRole("region", {name: "Profile load progress"})).toBeVisible();
});

test("saves the current numbered draft with recipe selectors and Spark IDs", async () => {
  const user = userEvent.setup();
  const autosaveProfile = vi.fn(async (_number: number, input: Record<string, unknown>) => ({...profile, revision: 5, name: input.name}));
  const api = apiFor({autosaveProfile: autosaveProfile as unknown as ControlApi["autosaveProfile"]});
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  const saved = await screen.findByRole("region", {name: "Profile 2 saved profile"});
  await user.click(within(saved).getByRole("button", {name: "Edit profile"}));
  await user.clear(screen.getByLabelText("Assignment name"));
  await user.type(screen.getByLabelText("Assignment name"), "coding");
  await user.click(screen.getByRole("button", {name: "Save profile"}));
  expect(autosaveProfile).toHaveBeenCalledWith(2, expect.objectContaining({expected_revision: 4, assignments: [expect.objectContaining({recipe_selector: "qwen-code", assignment_name: "coding", spark_ids: [nodeA]})]}));
});

test("renders an empty profile as an explicit whole-fleet idle outcome", async () => {
  const empty = {...profile, number: 3, name: "Idle", assignments: []} as FleetProfile;
  const api = apiFor({profiles: vi.fn(async () => ({schema_version: 2 as const, generated_at: "2026-09-10T00:00:00Z", profiles: [empty]}))});
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  const saved = await screen.findByRole("region", {name: "Profile 3 saved profile"});
  expect(within(saved).getByText("Idle fleet")).toBeVisible();
  expect(within(saved).getByText("No assignments; every Spark becomes idle on load.")).toBeVisible();
});
