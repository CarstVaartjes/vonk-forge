import {render, screen, within} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import type {ControlApi, FleetProfile} from "../api/types";
import {fullLibraryDetail} from "../test-fixtures/library";
import {LibraryNodeNamesProvider} from "./library-node-names";
import {LibraryProfileComposer} from "./library-profile-composer";

const savedProfile: FleetProfile = {
  schema_version: 2,
  id: "00000000-0000-4000-8000-000000000001",
  name: "Qwen Chat ready",
  description: "Keep Qwen Chat ready on its selected Spark group.",
  installation_policy: "keep-cached",
  labels: {source: "library"},
  favorite: true,
  scope: {node_ids: ["node-alpha", "node-beta"]},
  assignments: [{
    id: "00000000-0000-4000-8000-000000000002",
    recipe_id: fullLibraryDetail.recipe.recipe_id,
    recipe_revision_id: fullLibraryDetail.selected_revision!.id,
    recipe_title: fullLibraryDetail.recipe.title,
    model_title: "Qwen3",
    topology_name: "pair",
    desired_state: "running",
    alias: "qwen-chat",
    nodes: [
      {node_id: "node-alpha", rank: 0, role: "leader", endpoint_owner: true},
      {node_id: "node-beta", rank: 1, role: "worker", endpoint_owner: false},
    ],
  }],
  profile_digest: "a".repeat(64),
  created_by: "admin",
  created_at: "2026-08-28T12:00:00Z",
  updated_at: "2026-08-28T12:00:00Z",
};

test("saves an eligible exact Library placement as a running Fleet Profile", async () => {
  const createFleetProfile = vi.fn(async () => savedProfile);
  const api = {
    createFleetProfile,
    fleetProfiles: async () => ({schema_version: 1, generated_at: "2026-08-28T12:00:00Z", profiles: []}),
    updateFleetProfile: vi.fn(),
  } as unknown as ControlApi;
  const user = userEvent.setup();
  render(<LibraryNodeNamesProvider names={{"node-alpha": "Spark Alpha", "node-beta": "Spark Beta"}}><LibraryProfileComposer api={api} detail={fullLibraryDetail}/></LibraryNodeNamesProvider>);

  await user.click(screen.getByRole("button", {name: "Add to Fleet Profile"}));
  expect(await screen.findByRole("heading", {name: "Add recipe to a Fleet Profile"})).toBeVisible();
  expect(screen.getByRole("option", {name: /Spark Alpha \+ Spark Beta · installed/i})).toBeInTheDocument();
  const ranks = screen.getByRole("list", {name: "Saved Spark rank order"});
  expect(within(ranks).getByText("Rank 0")).toBeVisible();
  expect(within(ranks).getByText("leader · endpoint owner")).toBeVisible();

  await user.click(screen.getByRole("button", {name: "Create Fleet Profile"}));

  expect(createFleetProfile).toHaveBeenCalledWith(expect.objectContaining({
    name: "Qwen Chat ready",
    favorite: true,
    assignments: [expect.objectContaining({
      recipe_revision_id: fullLibraryDetail.selected_revision!.id,
      topology_name: "pair",
      desired_state: "running",
      alias: "qwen-chat",
      nodes: [
        {node_id: "node-alpha", rank: 0, role: "leader", endpoint_owner: true},
        {node_id: "node-beta", rank: 1, role: "worker", endpoint_owner: false},
      ],
    })],
  }));
  expect(await screen.findByText("Qwen Chat ready is ready")).toBeVisible();
  expect(screen.getByRole("link", {name: "Review in Fleet"})).toHaveAttribute("href", "/fleet");
});
