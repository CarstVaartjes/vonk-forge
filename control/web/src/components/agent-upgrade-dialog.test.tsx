import {act, fireEvent, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {AgentRepairManifest, AgentUpgradePlan, AgentUpgradeStrategy, ControlApi} from "../api/types";
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
const SELECTED_NODE = `spk_${"a".repeat(32)}`;
const REPAIR_AUTHORITY = "1".repeat(64);
const REPAIR_PACKAGE_SHA = "2".repeat(64);
const REPAIR_MANIFEST: AgentRepairManifest = {
  schema_version: 1,
  kind: "agent-upgrade-repair",
  node_id: SELECTED_NODE,
  authority_sha256: REPAIR_AUTHORITY,
  package: {
    architecture: "linux-arm64",
    package_bytes: 6_000_000,
    package_sha256: REPAIR_PACKAGE_SHA,
    package_signature: "8".repeat(128),
    package_url: `https://install.vonkforge.ai/repair-capsules/${SELECTED_NODE}/${REPAIR_AUTHORITY}/${REPAIR_PACKAGE_SHA}/vonk-forge-agent.deb`,
    package_version: "0.1.0~dev.382+gd1cef9c7d1ce",
    schema_version: 1,
    target_binary_digest: "a".repeat(64),
    target_build_digest: `sha256:${"9".repeat(64)}`,
  },
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

test("previews full repair evidence and requires exact Spark confirmation", async () => {
  const previews: Array<AgentRepairManifest | undefined> = [];
  const applied: AgentUpgradePlan[] = [];
  const api = {
    previewAgentUpgrade: async (nodeIds: string[] | undefined, strategy: AgentUpgradeStrategy, repair?: AgentRepairManifest) => {
      previews.push(repair);
      const base = plan(nodeIds ?? [], strategy);
      return repair ? {
        ...base,
        package: repair.package,
        repair_manifest: repair,
      } : base;
    },
    applyAgentUpgrade: async (upgradePlan: AgentUpgradePlan) => { applied.push(upgradePlan); return {id: "repair-job", state: "queued"}; },
  } as unknown as ControlApi;
  render(<AgentUpgradeDialog api={api} node={{id: SELECTED_NODE, name: "Spark 3542"}} onClose={() => undefined}/>);
  await screen.findByRole("button", {name: "Upgrade this Spark"});

  await userEvent.click(screen.getByText("Advanced: signed repair capsule"));
  await userEvent.click(screen.getByRole("checkbox", {name: "Use a node-bound repair manifest"}));
  fireEvent.change(screen.getByRole("textbox", {name: "Repair manifest JSON"}), {target: {value: JSON.stringify(REPAIR_MANIFEST)}});

  expect(await screen.findByRole("region", {name: "Agent repair preview"})).toBeVisible();
  expect(screen.getAllByText(SELECTED_NODE).length).toBeGreaterThan(0);
  expect(screen.getByText(REPAIR_AUTHORITY)).toBeVisible();
  expect(screen.getByText(REPAIR_MANIFEST.package.package_url)).toBeVisible();
  expect(screen.getByText(REPAIR_MANIFEST.package.package_signature)).toBeVisible();
  expect(screen.getByText(REPAIR_MANIFEST.package.target_binary_digest)).toBeVisible();
  expect(screen.getByText(REPAIR_MANIFEST.package.target_build_digest)).toBeVisible();
  const repairButton = screen.getByRole("button", {name: "Repair Spark 3542"});
  expect(repairButton).toBeDisabled();
  await userEvent.type(screen.getByRole("textbox", {name: "Repair confirmation"}), "Repair Spark 3542");
  expect(repairButton).toBeEnabled();
  await userEvent.click(repairButton);

  await waitFor(() => expect(applied).toHaveLength(1));
  expect(applied[0].repair_manifest).toEqual(REPAIR_MANIFEST);
  expect(previews.at(-1)).toEqual(REPAIR_MANIFEST);
  expect(await screen.findByText("Repair queued")).toBeVisible();
});

test("never previews a wrong-node or edited repair manifest", async () => {
  const repairs: AgentRepairManifest[] = [];
  const api = {
    previewAgentUpgrade: async (nodeIds: string[] | undefined, strategy: AgentUpgradeStrategy, repair?: AgentRepairManifest) => {
      if (repair) repairs.push(repair);
      return plan(nodeIds ?? [], strategy);
    },
  } as unknown as ControlApi;
  render(<AgentUpgradeDialog api={api} node={{id: SELECTED_NODE, name: "Spark 3542"}} onClose={() => undefined}/>);
  await screen.findByRole("button", {name: "Upgrade this Spark"});
  await userEvent.click(screen.getByText("Advanced: signed repair capsule"));
  await userEvent.click(screen.getByRole("checkbox", {name: "Use a node-bound repair manifest"}));
  const textarea = screen.getByRole("textbox", {name: "Repair manifest JSON"});
  fireEvent.change(textarea, {target: {value: JSON.stringify({...REPAIR_MANIFEST, node_id: `spk_${"b".repeat(32)}`})}});
  expect(await screen.findByRole("alert")).toHaveTextContent("not spk_aaaaaaaa");
  expect(repairs).toEqual([]);

  fireEvent.change(textarea, {target: {value: JSON.stringify(REPAIR_MANIFEST)}});
  await waitFor(() => expect(repairs).toEqual([REPAIR_MANIFEST]));
  fireEvent.change(textarea, {target: {value: JSON.stringify({...REPAIR_MANIFEST, unexpected: true})}});
  expect(await screen.findByRole("alert")).toHaveTextContent("missing or unknown");
  expect(screen.queryByRole("region", {name: "Agent repair preview"})).not.toBeInTheDocument();
  expect(repairs).toEqual([REPAIR_MANIFEST]);
});

test("recovers a failed preview without closing the dialog", async () => {
  let attempts = 0;
  const api = {
    previewAgentUpgrade: async (nodeIds: string[] | undefined, strategy: AgentUpgradeStrategy) => {
      attempts += 1;
      if (attempts === 1) throw new Error("Release service is temporarily unavailable.");
      return plan(nodeIds ?? [SELECTED_NODE], strategy);
    },
  } as unknown as ControlApi;
  render(<AgentUpgradeDialog api={api} node={{id: SELECTED_NODE, name: "Spark 3542"}} onClose={() => undefined}/>);

  expect(await screen.findByRole("alert")).toHaveTextContent("temporarily unavailable");
  await userEvent.click(screen.getByRole("button", {name: "Try preview again"}));

  expect(await screen.findByRole("button", {name: "Upgrade this Spark"})).toBeEnabled();
  expect(attempts).toBe(2);
});

test("rejects an oversized manifest file and queues at most once", async () => {
  let finishApply: ((value: {id: string; state: string}) => void) | undefined;
  const applyAgentUpgrade = vi.fn(() => new Promise<{id: string; state: string}>(resolve => { finishApply = resolve; }));
  const api = {
    previewAgentUpgrade: async (nodeIds: string[] | undefined, strategy: AgentUpgradeStrategy, repair?: AgentRepairManifest) => {
      const base = plan(nodeIds ?? [], strategy);
      return repair ? {...base, package: repair.package, repair_manifest: repair} : base;
    },
    applyAgentUpgrade,
  } as unknown as ControlApi;
  render(<AgentUpgradeDialog api={api} node={{id: SELECTED_NODE, name: "Spark 3542"}} onClose={() => undefined}/>);
  const close = await screen.findByRole("button", {name: "Close Upgrade Spark 3542"});
  expect(close).toHaveFocus();
  await userEvent.click(screen.getByText("Advanced: signed repair capsule"));
  await userEvent.click(screen.getByRole("checkbox", {name: "Use a node-bound repair manifest"}));
  fireEvent.change(screen.getByRole("textbox", {name: "Repair manifest JSON"}), {target: {value: JSON.stringify(REPAIR_MANIFEST)}});
  expect(await screen.findByRole("region", {name: "Agent repair preview"})).toBeVisible();
  await userEvent.type(screen.getByRole("textbox", {name: "Repair confirmation"}), "Repair Spark 3542");
  expect(screen.getByRole("button", {name: "Repair Spark 3542"})).toBeEnabled();
  await userEvent.upload(screen.getByLabelText("Or choose a JSON file"), new File(["x".repeat(256 * 1024 + 1)], "repair.json", {type: "application/json"}));
  expect(await screen.findByText("The repair manifest is larger than 256 KiB.")).toBeVisible();
  expect(screen.queryByRole("region", {name: "Agent repair preview"})).not.toBeInTheDocument();
  expect(screen.queryByRole("button", {name: "Repair Spark 3542"})).not.toBeInTheDocument();

  fireEvent.change(screen.getByRole("textbox", {name: "Repair manifest JSON"}), {target: {value: JSON.stringify(REPAIR_MANIFEST)}});
  expect(await screen.findByRole("region", {name: "Agent repair preview"})).toBeVisible();
  await userEvent.type(screen.getByRole("textbox", {name: "Repair confirmation"}), "Repair Spark 3542");
  let rejectRead: ((reason?: unknown) => void) | undefined;
  const pendingRead = new Promise<string>((_resolve, reject) => { rejectRead = reject; });
  const unreadable = new File(["{}"], "unreadable.json", {type: "application/json"});
  Object.defineProperty(unreadable, "text", {value: vi.fn(() => pendingRead)});
  fireEvent.change(screen.getByLabelText("Or choose a JSON file"), {target: {files: [unreadable]}});
  await waitFor(() => expect(screen.queryByRole("region", {name: "Agent repair preview"})).not.toBeInTheDocument());
  expect(screen.queryByRole("button", {name: "Repair Spark 3542"})).not.toBeInTheDocument();
  await act(async () => { rejectRead?.(new Error("read failed")); });
  expect(await screen.findByText("The repair manifest file could not be read. Choose it again or paste the JSON.")).toBeVisible();
  expect(screen.queryByRole("region", {name: "Agent repair preview"})).not.toBeInTheDocument();
  expect(screen.queryByRole("button", {name: "Repair Spark 3542"})).not.toBeInTheDocument();

  await userEvent.click(screen.getByRole("checkbox", {name: "Use a node-bound repair manifest"}));
  const upgrade = await screen.findByRole("button", {name: "Upgrade this Spark"});
  fireEvent.click(upgrade);
  fireEvent.click(upgrade);
  expect(applyAgentUpgrade).toHaveBeenCalledOnce();
  finishApply?.({id: "job-once", state: "queued"});
  expect(await screen.findByText("Upgrade queued")).toBeVisible();
});
