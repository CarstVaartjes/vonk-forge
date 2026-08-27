import {render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {AgentUpgradePlan, AgentUpgradeStrategy, ControlApi} from "../api/types";
import {AgentUpgradeDialog} from "./agent-upgrade-dialog";

const PACKAGE: AgentUpgradePlan["package"] = {
  architecture: "linux-arm64",
  package_bytes: 5 * 1024 * 1024,
  package_sha256: "d".repeat(64),
  package_signature: "e".repeat(128),
  package_url: "https://install.vonkforge.ai/artifacts/dev/example/vonk-forge-agent.deb",
  package_version: "0.1.0~dev.330+g0123456789ab",
  schema_version: 1,
  target_binary_digest: "a".repeat(64),
  target_build_digest: `sha256:${"b".repeat(64)}`,
};

function plan(nodeIds: string[], strategy: AgentUpgradePlan["strategy"]): AgentUpgradePlan {
  return {
    authority_revision: "c".repeat(64),
    node_ids: nodeIds,
    package: PACKAGE,
    plan_digest: "f".repeat(64),
    strategy,
  };
}

test("previews and applies a controller-selected all-at-once Fleet rollout", async () => {
  const previews: Array<{nodeIds: string[] | undefined; strategy: string}> = [];
  const applied: AgentUpgradePlan[] = [];
  const api = {
    previewAgentUpgrade: async (nodeIds: string[] | undefined, strategy: AgentUpgradeStrategy) => {
      previews.push({nodeIds, strategy});
      return plan(["spk_a", "spk_b"], strategy);
    },
    applyAgentUpgrade: async (upgradePlan: AgentUpgradePlan) => {
      applied.push(upgradePlan);
      return {id: "job-1", state: "queued"};
    },
  } as unknown as ControlApi;
  render(<AgentUpgradeDialog api={api} onClose={() => undefined}/>);

  expect(await screen.findByText("2 Sparks")).toBeVisible();
  await userEvent.selectOptions(screen.getByLabelText("Rollout strategy"), "all-at-once");
  await waitFor(() => expect(previews.at(-1)).toEqual({nodeIds: undefined, strategy: "all-at-once"}));
  await userEvent.click(screen.getByRole("button", {name: "Start rollout"}));

  await waitFor(() => expect(applied).toHaveLength(1));
  expect(applied[0].strategy).toBe("all-at-once");
  expect(await screen.findByText("Upgrade queued")).toBeVisible();
});

test("targets only the Spark selected from its detail screen", async () => {
  const previews: Array<string[] | undefined> = [];
  const api = {
    previewAgentUpgrade: async (nodeIds: string[] | undefined, strategy: AgentUpgradeStrategy) => {
      previews.push(nodeIds);
      return plan(nodeIds ?? [], strategy);
    },
    applyAgentUpgrade: async () => ({id: "job-2", state: "queued"}),
  } as unknown as ControlApi;
  render(<AgentUpgradeDialog api={api} node={{id: "spk_selected", name: "Spark 2297"}} onClose={() => undefined}/>);

  expect(await screen.findByRole("dialog", {name: "Upgrade Spark 2297"})).toBeVisible();
  expect(previews).toEqual([["spk_selected"]]);
  expect(screen.queryByLabelText("Rollout strategy")).not.toBeInTheDocument();
  expect(screen.getByRole("button", {name: "Upgrade this Spark"})).toBeEnabled();
});
