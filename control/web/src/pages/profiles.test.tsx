import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {components} from "../api/generated";
import type {ControlApi} from "../api/types";
import {ProfilesPage} from "./profiles";

type Plan = components["schemas"]["ReconciliationPlanResponse"];
const profileId = "production-agents";
const plan: Plan = {
  agent_protocol_range: [4, 4], commit: "a".repeat(40), digest: "d".repeat(64),
  fleet_evidence_digest: "e".repeat(64),
  input_digests: {}, operation_graph: {base_commit: "a".repeat(40), nodes: [], schema_version: 1, targets: []},
  placements: {}, reconciliation_id: "22222222-2222-4222-8222-222222222222", releases: {}, routes: {}, targets: [],
};

it("loads the profile-scoped server plan and fleet gates without local planning", async () => {
  // Break caught: the web workflow creates a parallel plan, requests a commit
  // supplied by the browser, or skips live fleet acceptance evidence.
  const planned: string[] = [];
  const api = {
    documents: async () => ({commit: plan.commit, documents: []}),
    fleetEvidence: async () => ({commit: plan.commit, evidence_digest: plan.fleet_evidence_digest, nodes: []}),
    planProfile: async (id: string) => { planned.push(id); return plan; },
  } as unknown as ControlApi;
  render(<ProfilesPage api={api}/>);
  const user = userEvent.setup();

  await user.type(screen.getByLabelText("Profile ID to reconcile"), profileId);
  await user.click(screen.getByRole("button", {name: "Preview exact plan"}));

  expect(planned).toEqual([profileId]);
  expect(await screen.findByText(plan.digest)).toBeVisible();
});
it("reports plan errors accessibly and does not retain a prior digest", async () => {
  // Break caught: a stale/conflicting plan error leaves an old digest applyable.
  let fail = false;
  const api = {
    documents: async () => ({commit: plan.commit, documents: []}),
    fleetEvidence: async () => ({commit: plan.commit, evidence_digest: plan.fleet_evidence_digest, nodes: []}),
    planProfile: async () => {
      if (fail) throw new Error("Control API returned 409: reconciliation plan digest is stale");
      return plan;
    },
  } as unknown as ControlApi;
  render(<ProfilesPage api={api}/>);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Profile ID to reconcile"), profileId);
  await user.click(screen.getByRole("button", {name: "Preview exact plan"}));
  expect(await screen.findByText(plan.digest)).toBeVisible();

  fail = true;
  await user.click(screen.getByRole("button", {name: "Preview exact plan"}));
  expect(await screen.findByRole("alert")).toHaveTextContent(/409/);
  expect(screen.queryByText(plan.digest)).not.toBeInTheDocument();
});
