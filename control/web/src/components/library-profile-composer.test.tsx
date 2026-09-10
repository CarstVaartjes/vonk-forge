import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import type {ControlApi, FleetProfile, LibraryRecipeDetail} from "../api/types";
import {LibraryNodeNamesProvider} from "./library-node-names";
import {LibraryProfileComposer} from "./library-profile-composer";

const nodeId = "spk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const detail = {recipe: {slug: "qwen-code", title: "Qwen Code"}, model_documents: [{model_document: {identity: {variant: "nvfp4"}}}], definition: {topology: {name: "single"}}, placement: [{recommendations: [{eligible: true, node_ids: [nodeId], nodes: [{node_id: nodeId, rank: 0, role: "leader", endpoint_owner: true}], topology_name: "single", load_state: "ready", install_state: "ready"}]}]} as unknown as LibraryRecipeDetail;
const saved = {number: 1, name: "Qwen Code ready"} as unknown as FleetProfile;

test("autosaves a recipe choice using the shared numbered Profile API", async () => {
  const user = userEvent.setup();
  const autosaveProfile = vi.fn(async () => saved);
  const api = {profiles: vi.fn(async () => ({profiles: []})), autosaveProfile} as unknown as ControlApi;
  render(<LibraryNodeNamesProvider names={{[nodeId]: "Spark Alpha"}}><LibraryProfileComposer api={api} detail={detail}/></LibraryNodeNamesProvider>);

  await user.click(screen.getByRole("button", {name: "Add to Fleet Profile"}));
  await screen.findByRole("heading", {name: "Add recipe to a Fleet Profile"});
  await user.click(screen.getByRole("button", {name: "Create Fleet Profile"}));

  expect(autosaveProfile).toHaveBeenCalledWith(1, expect.objectContaining({assignments: [expect.objectContaining({recipe_selector: "qwen-code", model_variant: "nvfp4", spark_ids: [nodeId]})]}));
  expect(await screen.findByText("Qwen Code ready is ready")).toBeVisible();
});
