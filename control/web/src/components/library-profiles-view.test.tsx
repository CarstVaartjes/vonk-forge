import {act, render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import {LosslessNumber} from "lossless-json";
import {ApiError} from "../api/client";
import type {ControlApi, FleetProfile, FleetProfileApplicationView, FleetProfilePreview} from "../api/types";
import {forgetUnsavedProfileDrafts, LibraryProfilesView} from "./library-profiles-view";
import {ToastProvider} from "./toast";

const nodeA = "spk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const nodeB = "spk_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const profile = {
  id: "11111111-1111-4111-8111-111111111111", number: 2, revision: 4,
  name: "Coding", description: "Code on Spark A", installation_policy: "keep-cached", labels: {}, favorite: true,
  definition: {name: "Coding", description: "Code on Spark A", installation_policy: "exact", labels: {team: "research"}, favorite: true, assignments: [{recipe_selector: "vonk-forge/qwen-code", spark_ids: [nodeA], assignment_name: "old-name", model_variant: "nvfp4", desired_state: "installed"}]},
  assignments: [{selector: "qwen-code", display_name: "Qwen Code", recipe_selector: "vonk-forge/qwen-code", recipe_id: null, spark_ids: [nodeA], required_sparks: 1, assigned_sparks: 1, model: {variant: "nvfp4", state: "cached"}, recipe: {selector: "vonk-forge/qwen-code", name: "Qwen Code", state: "cached", revision_id: "33333333-3333-4333-8333-333333333333"}, resources: {}, observed_state: "Not loaded"}],
  fleet: [{selector: nodeA, display_name: "Spark A", state: "idle"}, {selector: nodeB, display_name: "Spark B", state: "idle"}], status: "ready", loaded_revision: null, cache_summary: {}, warnings: [], next_actions: [], profile_digest: "a".repeat(64), created_by: "admin", created_at: "2026-09-10T00:00:00Z", updated_at: "2026-09-10T00:00:00Z",
} as unknown as FleetProfile;
const preview = {effects: {runs: [], installations: [], superseded: [], adopted: []}, allowed: true, plan_digest: "b".repeat(64), steps: [{index: 0, kind: "switch", label: "Switch to Qwen Code", node_ids: [nodeA]}], reasons: []} as unknown as FleetProfilePreview;
const application = {state: "running", progress: {child_progress: {phase: "start", node_ids: [nodeA], bytes: 50, total_bytes: 100}}, status_reason: null} as unknown as FleetProfileApplicationView;

beforeEach(forgetUnsavedProfileDrafts);

function apiFor(overrides: Partial<ControlApi> = {}): ControlApi {
  return {profiles: vi.fn(async () => ({generated_at: "2026-09-10T00:00:00Z", profiles: [profile]})), previewProfile: vi.fn(async () => preview), autosaveProfile: vi.fn(async () => profile), loadProfile: vi.fn(async () => application), profileApplicationByRequest: vi.fn(async () => application), profileProgress: vi.fn(async () => application), ...overrides} as unknown as ControlApi;
}

test("reads and loads a numbered profile without legacy status or application routes", async () => {
  const user = userEvent.setup();
  const api = apiFor();
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  expect((await screen.findAllByText("Profile 2 · Coding"))[0]).toBeVisible();
  await user.click(await screen.findByRole("button", {name: "Apply profile"}));
  expect(api.previewProfile).toHaveBeenCalledWith(2, expect.any(AbortSignal));
  expect(api.loadProfile).toHaveBeenCalledWith(2, {request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)});
  expect(await screen.findByRole("region", {name: "Profile load progress"})).toBeVisible();
});

test("a load names the reviewed effects, and a changed plan is refused with the current plan shown", async () => {
  const user = userEvent.setup();
  const reviewed = {...preview, effects_digest: "e".repeat(64)} as FleetProfilePreview;
  const changed = {...preview, effects_digest: "f".repeat(64), steps: [{index: 0, kind: "switch", label: "Stop Qwen Code first", node_ids: [nodeA]}]} as unknown as FleetProfilePreview;
  const previewProfile = vi.fn(async (..._args: Parameters<ControlApi["previewProfile"]>) => reviewed);
  previewProfile.mockResolvedValueOnce(reviewed).mockResolvedValue(changed);
  const loadProfile = vi.fn(async (..._args: Parameters<ControlApi["loadProfile"]>) => application);
  loadProfile.mockRejectedValueOnce(new ApiError(409, "The profile plan changed since it was reviewed"));
  const api = apiFor({previewProfile, loadProfile});
  render(<ToastProvider><LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/></ToastProvider>);
  await user.click(await screen.findByRole("button", {name: "Apply profile"}));

  expect(loadProfile).toHaveBeenCalledWith(2, {request_key: expect.stringMatching(/^[0-9a-f-]{36}$/), reviewed_effects_digest: "e".repeat(64)});
  await vi.waitFor(() => expect(previewProfile).toHaveBeenCalledTimes(2));
  await act(async () => { await Promise.resolve(); });

  await user.click(await screen.findByRole("button", {name: "Apply profile"}));
  expect(loadProfile).toHaveBeenLastCalledWith(2, {request_key: expect.stringMatching(/^[0-9a-f-]{36}$/), reviewed_effects_digest: "f".repeat(64)});
});

test("reconciles an ambiguous profile load with the same request key", async () => {
  const user = userEvent.setup();
  const loadProfile = vi.fn(async (..._args: Parameters<ControlApi["loadProfile"]>) => application);
  loadProfile.mockRejectedValueOnce(new TypeError("connection lost"));
  const profileApplicationByRequest = vi.fn(async () => {
    throw new ApiError(404, "Profile request not found");
  });
  const api = apiFor({loadProfile, profileApplicationByRequest});
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  await user.click(await screen.findByRole("button", {name: "Apply profile"}));

  expect(await screen.findByRole("region", {name: "Profile load progress"})).toBeVisible();
  expect(profileApplicationByRequest).toHaveBeenCalledWith(2, expect.stringMatching(/^[0-9a-f-]{36}$/));
  expect(loadProfile).toHaveBeenCalledTimes(2);
  expect(loadProfile.mock.calls[0]).toEqual(loadProfile.mock.calls[1]);
  expect(loadProfile.mock.calls[0]?.[1]).toEqual({
    request_key: expect.stringMatching(/^[0-9a-f-]{36}$/),
  });
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
  expect(autosaveProfile).toHaveBeenCalledWith(2, expect.objectContaining({expected_revision: 4, installation_policy: "exact", labels: {team: "research"}, assignments: [expect.objectContaining({recipe_selector: "vonk-forge/qwen-code", assignment_name: "coding", spark_ids: [nodeA], model_variant: "nvfp4", desired_state: "installed"})]}));
});

test("renders an empty profile as an explicit whole-fleet idle outcome", async () => {
  const empty = {...profile, number: 3, name: "Idle", assignments: []} as FleetProfile;
  const api = apiFor({profiles: vi.fn(async () => ({generated_at: "2026-09-10T00:00:00Z", profiles: [empty]}))});
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  const saved = await screen.findByRole("region", {name: "Profile 3 saved profile"});
  expect(within(saved).getByText("Idle fleet")).toBeVisible();
  expect(within(saved).getByText("No assignments; every Spark becomes idle on load.")).toBeVisible();
});

test("shows a refused profile load without resubmitting it", async () => {
  const user = userEvent.setup();
  const loadProfile = vi.fn(async () => application);
  loadProfile.mockRejectedValueOnce(new ApiError(409, "Profile admission refused"));
  const api = apiFor({loadProfile});
  render(<ToastProvider><LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/></ToastProvider>);
  await user.click(await screen.findByRole("button", {name: "Apply profile"}));
  expect(await within(screen.getByRole("region", {name: "Notifications"})).findByText(/Profile admission refused/)).toBeVisible();
  expect(loadProfile).toHaveBeenCalledTimes(1);
});

test("a loaded profile names the inference gateway and alias as its client endpoint", async () => {
  const loaded = {...profile, status: "loaded", loaded_revision: 4} as unknown as FleetProfile;
  const profileEndpoints = vi.fn(async () => ({
    number: 2, profile_id: profile.id, application_id: "22222222-2222-4222-8222-222222222222", application_state: "succeeded", observed_at: "2026-09-10T00:00:00Z",
    assignments: [{assignment_id: "33333333-3333-4333-8333-333333333333", recipe_title: "Qwen Code", desired_state: "running", alias: "qwen-code", state: "published", endpoint: {alias: "qwen-code", api_base: "https://vonk-forge.example.ts.net/v1", backend_api_base: "http://192.168.1.211:8888/v1", generation: 3, node_id: nodeA, observed_at: "2026-09-10T00:00:00Z", plan_digest: "c".repeat(64)}}],
  })) as unknown as ControlApi["profileEndpoints"];
  const api = apiFor({profiles: vi.fn(async () => ({generated_at: "2026-09-10T00:00:00Z", profiles: [loaded]})), profileEndpoints});
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  const endpoint = await screen.findByRole("region", {name: "Qwen Code client endpoint"});
  expect(within(endpoint).getByText("https://vonk-forge.example.ts.net/v1")).toBeVisible();
  expect(within(endpoint).getByText("qwen-code")).toBeVisible();
  expect(within(endpoint).getByText("Spark backend (diagnostic)")).toBeVisible();
  expect(profileEndpoints).toHaveBeenCalledWith(2, expect.any(AbortSignal));
});

test("a saved profile shows the cache counts the server reports", async () => {
  const cached = {...profile, cache_summary: {cached: 2, missing: 1, unknown: 0}} as unknown as FleetProfile;
  render(<LibraryProfilesView api={apiFor({profiles: vi.fn(async () => ({schema_version: 2 as const, generated_at: "2026-09-10T00:00:00Z", profiles: [cached]}))})} entries={[]} onNavigate={vi.fn()}/>);
  expect(await screen.findByText("2 cached · 1 missing")).toBeVisible();
});

test("a load waiting for preparation is offered, and a waiting application says what it waits for", async () => {
  const user = userEvent.setup();
  const waitingPreview = {...preview, allowed: false, waits_for_preparation: true, preparation_steps: [{index: 0, kind: "prepare", label: "Build the runtime image for Qwen Code (skipped when built)", node_ids: [nodeA]}], reasons: [{code: "profile.preparation_unavailable", detail: "Prepare the model first", severity: "error"}]} as unknown as FleetProfilePreview;
  const waiting = {state: "queued", progress: {}, status_reason: "Waiting to retry", blockers: [{code: "run-switch.inventory-unknown", detail: "No authenticated Spark inventory is available.", severity: "error", node_ids: [nodeA]}], next_attempt_at: "2026-09-29T12:00:00+00:00"} as unknown as FleetProfileApplicationView;
  const api = apiFor({previewProfile: vi.fn(async () => waitingPreview), loadProfile: vi.fn(async () => waiting)});
  render(<ToastProvider><LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/></ToastProvider>);
  expect(await screen.findByText(/The Controller prepares this first/)).toBeVisible();
  await user.click(await screen.findByRole("button", {name: "Apply profile"}));
  const list = await screen.findByRole("list", {name: "Waiting for"});
  expect(within(list).getByText("run-switch.inventory-unknown")).toBeVisible();
  expect(within(list).getByText(/Next attempt/)).toBeVisible();
});

test("a profile running an older recipe revision shows an update badge and reloads only after confirmation", async () => {
  const user = userEvent.setup();
  const detail = "Update available: running Qwen Code 1.6.0, newest 1.7.0 (2026-09-28). Reload to apply.";
  const outdated = {...profile, assignments: profile.assignments.map(item => ({...item, recipe_update: {code: "recipe.update_available", severity: "info", running_revision_id: "r1", newest_revision_id: "r2", detail}}))} as unknown as FleetProfile;
  const api = apiFor({profiles: vi.fn(async () => ({generated_at: "2026-09-10T00:00:00Z", profiles: [outdated]}))});
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);

  const notice = await screen.findByRole("region", {name: "Recipe updates available"});
  expect(within(notice).getByText("update available")).toBeVisible();
  expect(within(notice).getByText(detail)).toBeVisible();
  expect(api.loadProfile).not.toHaveBeenCalled();

  await user.click(within(notice).getByRole("button", {name: "Reload profile"}));
  expect(api.loadProfile).not.toHaveBeenCalled();
  const dialog = await screen.findByRole("alertdialog");
  await user.click(within(dialog).getByRole("button", {name: "Reload profile"}));
  expect(api.loadProfile).toHaveBeenCalledWith(2, {request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)});
});

test("a current profile shows no update notice", async () => {
  render(<LibraryProfilesView api={apiFor()} entries={[]} onNavigate={vi.fn()}/>);
  await screen.findAllByText("Profile 2 · Coding");
  expect(screen.queryByRole("region", {name: "Recipe updates available"})).toBeNull();
});

test("a completed load refreshes the saved profile and shows the endpoint without a reload, even after a failed progress poll", async () => {
  const user = userEvent.setup();
  const loaded = {...profile, status: "loaded", loaded_revision: 4} as unknown as FleetProfile;
  const profiles = vi.fn()
    .mockResolvedValueOnce({generated_at: "2026-09-10T00:00:00Z", profiles: [profile]})
    .mockResolvedValue({generated_at: "2026-09-10T00:01:00Z", profiles: [loaded]});
  const profileProgress = vi.fn()
    .mockRejectedValueOnce(new TypeError("connection lost"))
    .mockResolvedValue({...application, state: "succeeded"});
  const profileEndpoints = vi.fn(async () => ({
    number: 2, profile_id: profile.id, application_id: "22222222-2222-4222-8222-222222222222", application_state: "succeeded", observed_at: "2026-09-10T00:00:00Z",
    assignments: [{assignment_id: "33333333-3333-4333-8333-333333333333", recipe_title: "Qwen Code", desired_state: "running", alias: "qwen-code", state: "published", endpoint: {alias: "qwen-code", api_base: "https://vonk-forge.example.ts.net/v1", backend_api_base: "http://192.168.1.211:8888/v1", generation: 3, node_id: nodeA, observed_at: "2026-09-10T00:00:00Z", plan_digest: "c".repeat(64)}}],
  })) as unknown as ControlApi["profileEndpoints"];
  const api = apiFor({profiles, profileProgress: profileProgress as unknown as ControlApi["profileProgress"], profileEndpoints});
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  await user.click(await screen.findByRole("button", {name: "Apply profile"}));
  expect(await screen.findByRole("region", {name: "Qwen Code client endpoint"}, {timeout: 8_000})).toBeVisible();
  expect(profileProgress.mock.calls.length).toBeGreaterThanOrEqual(2);
}, 15_000);

test("a running profile application does not lock navigation", async () => {
  const user = userEvent.setup();
  const onBusyChange = vi.fn();
  render(<LibraryProfilesView api={apiFor()} entries={[]} onBusyChange={onBusyChange} onNavigate={vi.fn()}/>);
  await user.click(await screen.findByRole("button", {name: "Apply profile"}));
  expect(await screen.findByRole("region", {name: "Profile load progress"})).toBeVisible();
  expect(onBusyChange).toHaveBeenLastCalledWith(false);
});

test("unsaved edits survive selecting another profile", async () => {
  const user = userEvent.setup();
  const second = {...profile, number: 3, name: "Other"} as FleetProfile;
  const api = apiFor({profiles: vi.fn(async () => ({generated_at: "2026-09-10T00:00:00Z", profiles: [profile, second]}))});
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  const saved = await screen.findByRole("region", {name: "Profile 2 saved profile"});
  await user.click(within(saved).getByRole("button", {name: "Edit profile"}));
  await user.clear(screen.getByLabelText("Assignment name"));
  await user.type(screen.getByLabelText("Assignment name"), "kept-edit");
  await user.click(screen.getByRole("button", {name: /Profile 3 · Other/}));
  await user.click(screen.getByRole("button", {name: /Profile 2 · Coding/}));
  expect(await screen.findByLabelText("Assignment name")).toHaveValue("kept-edit");
});

test("the profile screen reviews the changes before Apply", async () => {
  const reviewed = {...preview, steps: [{index: 0, kind: "switch", label: "Switch Spark A to Qwen Code", node_ids: [nodeA]}], summary: {starts: 1, stops: 0, installs: 0, uninstalls: 0, builds: 0, distributions: 0, placements: 0, already_correct: 0, blockers: 0}} as unknown as FleetProfilePreview;
  render(<LibraryProfilesView api={apiFor({previewProfile: vi.fn(async () => reviewed)})} entries={[]} onNavigate={vi.fn()}/>);
  const review = await screen.findByRole("region", {name: "Review changes"});
  expect(within(review).getByText("Switch Spark A to Qwen Code")).toBeVisible();
  expect(within(review).getByText("1 start")).toBeVisible();
  expect(within(review).getByText("Technical detail")).toBeVisible();
});

test("a profile named in the URL is selected", async () => {
  const second = {...profile, number: 3, name: "Other"} as FleetProfile;
  window.history.replaceState(null, "", "/library/profiles?profile=3");
  try {
    render(<LibraryProfilesView api={apiFor({profiles: vi.fn(async () => ({generated_at: "2026-09-10T00:00:00Z", profiles: [profile, second]}))})} entries={[]} onNavigate={vi.fn()}/>);
    expect(await screen.findByRole("region", {name: "Profile 3 saved profile"})).toBeVisible();
  } finally { window.history.replaceState(null, "", "/"); }
});


test("an unreadable saved definition stays visible without becoming an editable empty profile", async () => {
  const unavailable = {
    id: "00000000-0000-4000-8000-000000000001", number: 1, revision: 8,
    status: "unavailable" as const, definition: null,
    projection_issue: {code: "profile.definition_unavailable" as const,
      detail: "The saved profile definition cannot be read. Its contents are unknown.",
      next_action: "Restore the saved definition or import a complete replacement."},
  };
  const api = apiFor({profiles: vi.fn(async () => ({generated_at: "2026-10-06T00:00:00Z", profiles: [unavailable, profile]}))});
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  expect(await screen.findByText("Profile 1 · Definition unavailable")).toBeVisible();
  expect(screen.getByText(unavailable.projection_issue.detail)).toBeVisible();
  expect(screen.queryByRole("button", {name: /Profile 1/})).not.toBeInTheDocument();
  expect((await screen.findAllByText("Profile 2 · Coding"))[0]).toBeVisible();
  expect(api.autosaveProfile).not.toHaveBeenCalled();
});


test("selects the maximum canonical URL profile and loads that identity", async () => {
  const user = userEvent.setup();
  const lower = 2147483646;
  const upper = 2147483647;
  const originalUrl = location.href;
  history.replaceState(null, "", "?profile=2147483647");
  try {
    const api = apiFor({profiles: vi.fn(async () => ({generated_at: "2026-09-10T00:00:00Z", profiles: [{...profile, number: upper, name: "Upper"}, {...profile, number: lower, name: "Lower"}]}))});
    render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
    const selected = await screen.findByRole("region", {name: "Profile 2147483647 saved profile"});
    const list = screen.getByRole("complementary", {name: "Saved profiles"});
    const buttons = within(list).getAllByRole("button");
    expect(buttons[0]).toHaveTextContent("Profile 2147483646 · Lower");
    expect(buttons[1]).toHaveAttribute("aria-pressed", "true");
    await user.click(within(selected).getByRole("button", {name: "Apply profile"}));
    expect(api.previewProfile).toHaveBeenCalledWith(upper, expect.any(AbortSignal));
    expect(api.loadProfile).toHaveBeenCalledWith(upper, {request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)});
  } finally {
    history.replaceState(null, "", originalUrl);
  }
});

test("editing a maximum canonical profile preserves its identity and revision", async () => {
  const user = userEvent.setup();
  const number = 2147483647;
  const revision = 2147483647;
  const exact = {...profile, number, revision};
  const autosaveProfile = vi.fn(async () => exact);
  const api = apiFor({profiles: vi.fn(async () => ({generated_at: "2026-09-10T00:00:00Z", profiles: [exact]})), autosaveProfile});
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  const saved = await screen.findByRole("region", {name: "Profile 2147483647 saved profile"});
  await user.click(within(saved).getByRole("button", {name: "Edit profile"}));
  await user.clear(screen.getByLabelText("Profile name"));
  await user.type(screen.getByLabelText("Profile name"), "Exact saved");
  await user.click(screen.getByRole("button", {name: "Save profile"}));
  expect(autosaveProfile).toHaveBeenCalledWith(number, expect.objectContaining({expected_revision: revision, name: "Exact saved"}));
});


test("refuses an out-of-domain URL profile through the canonical path contract", async () => {
  const originalUrl = location.href;
  history.replaceState(null, "", "?profile=2147483648");
  try {
    const api = apiFor();
    render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
    expect(await screen.findByRole("alert")).toBeVisible();
    expect(api.previewProfile).not.toHaveBeenCalled();
    expect(api.loadProfile).not.toHaveBeenCalled();
  } finally {
    history.replaceState(null, "", originalUrl);
  }
});


test("preserves legitimate wide byte progress while presenting its ratio", async () => {
  const user = userEvent.setup();
  const completed = new LosslessNumber("9007199254740992");
  const total = new LosslessNumber("18014398509481984");
  const wide = {...application, progress: {child_progress: {phase: "copy", node_ids: [nodeA], bytes: completed, total_bytes: total}}} as unknown as FleetProfileApplicationView;
  const api = apiFor({loadProfile: vi.fn(async () => wide), profileProgress: vi.fn(async () => wide)});
  render(<LibraryProfilesView api={api} entries={[]} onNavigate={vi.fn()}/>);
  await user.click(await screen.findByRole("button", {name: "Apply profile"}));
  expect(await screen.findByRole("progressbar", {name: "Profile load progress"})).toHaveAttribute("aria-valuenow", "50");
  expect(completed.value).toBe("9007199254740992");
  expect(total.value).toBe("18014398509481984");
});
