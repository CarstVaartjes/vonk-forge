import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
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
