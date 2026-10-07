import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import {LosslessNumber} from "lossless-json";
import type {ControlApi, FleetProfile, LibraryViewRecipeDetail} from "../api/types";
import {LibraryNodeNamesProvider} from "./library-node-names";
import {LibraryProfileComposer} from "./library-profile-composer";

const nodeId = "spk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const detail = {recipe: {publisher: "vonk-forge", slug: "qwen-code", title: "Qwen Code"}, model_documents: [{model_document: {identity: {variant: "nvfp4"}}}], definition: {topology: {name: "single"}}, placement: [{recommendations: [{eligible: true, node_ids: [nodeId], nodes: [{node_id: nodeId, rank: 0, role: "leader", endpoint_owner: true}], topology_name: "single", load_state: "ready", install_state: "ready"}]}]} as unknown as LibraryViewRecipeDetail;
const saved = {number: 1, name: "Qwen Code ready"} as unknown as FleetProfile;

test("autosaves a recipe choice using the shared numbered Profile API", async () => {
  const user = userEvent.setup();
  const autosaveProfile = vi.fn(async () => saved);
  const api = {profiles: vi.fn(async () => ({profiles: []})), autosaveProfile} as unknown as ControlApi;
  render(<LibraryNodeNamesProvider names={{[nodeId]: "Spark Alpha"}}><LibraryProfileComposer api={api} detail={detail}/></LibraryNodeNamesProvider>);

  await user.click(screen.getByRole("button", {name: "Add to Fleet Profile"}));
  await screen.findByRole("heading", {name: "Add recipe to a Fleet Profile"});
  await user.click(screen.getByRole("button", {name: "Create Fleet Profile"}));

  expect(autosaveProfile).toHaveBeenCalledWith(1, expect.objectContaining({assignments: [expect.objectContaining({recipe_selector: "vonk-forge/qwen-code", model_variant: "nvfp4", spark_ids: [nodeId]})]}));
  expect(await screen.findByText("Qwen Code ready is ready")).toBeVisible();
});

test("adding a recipe preserves the saved definition of existing assignments", async () => {
  const user = userEvent.setup();
  const definition = {name: "Stored", description: "Keep this", favorite: false, installation_policy: "exact", labels: {use: "draft"}, assignments: [{recipe_selector: "vonk-forge/other-recipe", spark_ids: [nodeId], assignment_name: "installed-draft", model_variant: "precise-variant", desired_state: "installed"}]};
  const existing = {number: 2, revision: 7, name: "Stored", status: "ready", definition, assignments: [{recipe_selector: "vonk-forge/other-recipe", spark_ids: [nodeId], observed_state: "Not loaded"}]} as unknown as FleetProfile;
  const autosaveProfile = vi.fn(async () => existing);
  const api = {profiles: vi.fn(async () => ({profiles: [existing]})), autosaveProfile} as unknown as ControlApi;
  render(<LibraryNodeNamesProvider names={{[nodeId]: "Spark Alpha"}}><LibraryProfileComposer api={api} detail={detail}/></LibraryNodeNamesProvider>);
  await user.click(screen.getByRole("button", {name: "Add to Fleet Profile"}));
  await screen.findByRole("option", {name: "Profile 2 · Stored · 1 workloads"});
  await waitFor(() => expect(screen.getByLabelText("Destination")).toBeEnabled());
  await user.selectOptions(screen.getByLabelText("Destination"), "2");
  await user.click(screen.getByRole("button", {name: "Add workload"}));
  expect(autosaveProfile).toHaveBeenCalledWith(2, {
    ...definition, expected_revision: 7,
    assignments: [definition.assignments[0], expect.objectContaining({recipe_selector: "vonk-forge/qwen-code", desired_state: "running"})],
  });
});

const options = [
  {name: "verification", label: "Verification", help: "How drafted tokens are verified.", choices: [{value: "standard", label: "Standard", help: "All.", default: true, args: [], env: {}}, {value: "adaptive-k", label: "Adaptive", help: "Prefix.", default: false, args: [], env: {}}]},
  {name: "projections", label: "Projections", help: "Dense weights.", choices: [{value: "stock", label: "Stock", help: "BF16.", default: true, args: [], env: {}}, {value: "dense-fp8", label: "FP8", help: "FP8.", default: false, args: [], env: {}}]},
];
const optioned = {...detail, definition: {topology: {name: "single"}, options}} as unknown as LibraryViewRecipeDetail;

test("untouched recipe options are saved explicitly with the recipe defaults", async () => {
  const user = userEvent.setup();
  const autosaveProfile = vi.fn(async () => saved);
  const api = {profiles: vi.fn(async () => ({profiles: []})), autosaveProfile} as unknown as ControlApi;
  render(<LibraryNodeNamesProvider names={{[nodeId]: "Spark Alpha"}}><LibraryProfileComposer api={api} detail={optioned}/></LibraryNodeNamesProvider>);
  await user.click(screen.getByRole("button", {name: "Add to Fleet Profile"}));
  expect(await screen.findByLabelText("Verification")).toHaveValue("standard");
  await user.click(screen.getByRole("button", {name: "Create Fleet Profile"}));
  expect(autosaveProfile).toHaveBeenCalledWith(1, expect.objectContaining({assignments: [expect.objectContaining({option_choices: {verification: "standard", projections: "stock"}})]}));
});

test("a chosen recipe option is saved with the assignment", async () => {
  const user = userEvent.setup();
  const autosaveProfile = vi.fn(async () => saved);
  const api = {profiles: vi.fn(async () => ({profiles: []})), autosaveProfile} as unknown as ControlApi;
  render(<LibraryNodeNamesProvider names={{[nodeId]: "Spark Alpha"}}><LibraryProfileComposer api={api} detail={optioned}/></LibraryNodeNamesProvider>);
  await user.click(screen.getByRole("button", {name: "Add to Fleet Profile"}));
  await user.selectOptions(await screen.findByLabelText("Verification"), "adaptive-k");
  await user.click(screen.getByRole("button", {name: "Create Fleet Profile"}));
  expect(autosaveProfile).toHaveBeenCalledWith(1, expect.objectContaining({assignments: [expect.objectContaining({option_choices: {verification: "adaptive-k", projections: "stock"}})]}));
});


test("selects adjacent large profile identities and preserves their exact revision", async () => {
  const user = userEvent.setup();
  const lower = new LosslessNumber("9007199254740992");
  const upper = new LosslessNumber("9007199254740993");
  const revision = new LosslessNumber("9007199254740995");
  const definition = {name: "Exact", description: "", favorite: false, installation_policy: "exact", labels: {}, assignments: []};
  const existing = {number: upper, revision, name: "Exact", definition, assignments: []} as unknown as FleetProfile;
  const autosaveProfile = vi.fn(async () => existing);
  const api = {profiles: vi.fn(async () => ({profiles: [{...existing, number: lower, name: "Lower"}, existing]})), autosaveProfile} as unknown as ControlApi;
  render(<LibraryProfileComposer api={api} detail={detail}/>);
  await user.click(screen.getByRole("button", {name: "Add to Fleet Profile"}));
  await screen.findByRole("option", {name: "Profile 9007199254740993 · Exact · 0 workloads"});
  await waitFor(() => expect(screen.getByLabelText("Destination")).toBeEnabled());
  await user.selectOptions(screen.getByLabelText("Destination"), "9007199254740993");
  await user.click(screen.getByRole("button", {name: "Add workload"}));
  expect(autosaveProfile).toHaveBeenCalledWith(upper, expect.objectContaining({expected_revision: revision}));
  expect(await screen.findByRole("link", {name: "Review and apply profile 9007199254740993"})).toHaveAttribute("href", "/library/profiles?profile=9007199254740993");
});

test("allocates the next profile number exactly beyond the safe integer range", async () => {
  const user = userEvent.setup();
  const largest = new LosslessNumber("9007199254740993");
  const next = new LosslessNumber("9007199254740994");
  const autosaveProfile = vi.fn(async () => ({...saved, number: next}));
  const api = {profiles: vi.fn(async () => ({profiles: [{number: largest, name: "Existing", definition: {}, assignments: []}]})), autosaveProfile} as unknown as ControlApi;
  render(<LibraryProfileComposer api={api} detail={detail}/>);
  await user.click(screen.getByRole("button", {name: "Add to Fleet Profile"}));
  await waitFor(() => expect(screen.getByLabelText("Destination")).toBeEnabled());
  await user.click(screen.getByRole("button", {name: "Create Fleet Profile"}));
  expect(autosaveProfile).toHaveBeenCalledWith(next, expect.any(Object));
});
