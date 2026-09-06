import {act, fireEvent, render, screen, waitFor} from "@testing-library/react";
import type {LibraryRecipeDetail, RunSwitchOperation, RunSwitchPlan} from "../api/types";
import {fullLibraryDetail, librarySnapshot} from "../test-fixtures/library";
import {LibraryRecipeAuthority} from "./library-recipe-detail";
import {LibraryNodeNamesProvider} from "./library-node-names";
import {LibraryRunSwitchProgress} from "./library-run-switch-progress";

const digest = "a".repeat(64);
const recipeRevisionId = "c".repeat(64);
const nodeA = "spk_" + "1".repeat(32);

const sourceGroup = {
  eligible: true,
  topology_name: "solo",
  node_ids: [nodeA],
  nodes: [{node_id: nodeA, rank: 0, role: "leader", endpoint_owner: true, artifact_reuse_bytes: 0, disk_free_after_bytes: 100, disk_free_bytes: 100, disk_required_bytes: 1, disk_reserved_bytes: 0, fabric_address: null, fabric_bandwidth_mbps: null, inventory_age_seconds: 1, inventory_observed_at: "2026-09-04T12:00:00Z", memory_available_bytes: 100, memory_free_after_bytes: 100, memory_kind: "host", memory_required_bytes: 1, memory_reserved_bytes: 0, telemetry_age_seconds: 1, telemetry_observed_at: "2026-09-04T12:00:00Z"}],
  preview_targets: [{kind: "run" as const, input: {installation_id: "installation-chat"}}],
  load_state: "not_loaded",
  install_state: "complete",
  reasons: [],
} as const;


function operation(overrides: Partial<RunSwitchOperation> = {}): RunSwitchOperation {
  return {
    schema_version: 2,
    operation_id: "11111111-1111-4111-8111-111111111111",
    kind: "recipe.run-switch.v2",
    action: "run",
    state: "running",
    plan_digest: digest,
    request_key: "22222222-2222-4222-8222-222222222222",
    node_ids: [nodeA],
    current_phase: "transfer",
    completed_phases: [],
    progress: {
      phase_index: 0,
      phase_count: 3,
      phase: "transfer",
      state: "running",
      completed_bytes: 0,
      total_bytes: null,
      total_bytes_known: false,
      members: [{node_id: nodeA, phase: "transfer", state: "running", completed_bytes: 0, total_bytes: null}],
    },
    ...overrides,
  };
}

const fit = {allowed: true, nodes: [{allowed: true, node_id: nodeA, rank: 0, role: "leader"}]};
const storage = {copied_bytes: 0, nas_coverage: "unknown" as const, reclaimable_bytes: 0, reclaimed_bytes: 0, required_bytes: null, retention: "retain-cached" as const, reused_bytes: 0, running_coverage: "unknown" as const, spark_coverage: "unknown" as const};
const runtimeStorage = {build_id: null, copied_bytes: 0, image_digest: null, nas_coverage: "unknown" as const, reclaimable_bytes: 0, reused_bytes: 0, running_coverage: "unknown" as const, spark_coverage: "unknown" as const};
const build = {build_id: null, compatibility: {expected_architecture: "linux/arm64", state: "unknown" as const}, image_digest: null, runtime: runtimeStorage, source: {state: "unknown" as const}, state: "unknown" as const};

test("keeps unknown byte progress indeterminate and polls the durable operation", async () => {
  vi.useFakeTimers();
  const next = operation({state: "succeeded", current_phase: "final_verify", progress: {...operation().progress, phase: "final_verify", state: "succeeded", completed_bytes: 0, members: [{node_id: nodeA, phase: "final_verify", state: "succeeded", completed_bytes: 0, total_bytes: null}]}});
  const getOperation = vi.fn(async () => next);
  render(<LibraryRunSwitchProgress api={{getRecipeRunSwitchOperation: getOperation, retryRecipeRunSwitch: vi.fn()}} nodeNames={{[nodeA]: "Spark One"}} onChange={vi.fn()} operation={operation()} title="Qwen Chat"/>);
  expect(screen.getByRole("progressbar", {name: "Run progress"})).not.toHaveAttribute("aria-valuenow");
  expect(screen.getAllByText("Total bytes unavailable").length).toBeGreaterThan(0);
  await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
  expect(getOperation).toHaveBeenCalledWith(operation().operation_id, expect.any(AbortSignal));
  vi.useRealTimers();
});

test("retries a transient Run through the durable endpoint and adopts its new operation", async () => {
  const failed = operation({state: "failed", status_reason: "temporary Spark transfer failure", result: {retryable: true}});
  const replacement = operation({operation_id: "33333333-3333-4333-8333-333333333333", state: "queued", result: {retryable: false}});
  const retryRecipeRunSwitch = vi.fn(async () => replacement);
  const onChange = vi.fn();
  vi.spyOn(crypto, "randomUUID").mockReturnValue("44444444-4444-4444-8444-444444444444");
  render(<LibraryRunSwitchProgress api={{getRecipeRunSwitchOperation: vi.fn(), retryRecipeRunSwitch}} nodeNames={{[nodeA]: "Spark One"}} onChange={onChange} operation={failed} title="Qwen Chat"/>);

  fireEvent.click(screen.getByRole("button", {name: "Retry run"}));
  await waitFor(() => expect(retryRecipeRunSwitch).toHaveBeenCalledWith(failed.operation_id, {schema_version: 2, request_key: "44444444-4444-4444-8444-444444444444"}));
  expect(onChange).toHaveBeenCalledWith(replacement);
});

test("does not offer Run recovery for terminal authorization or integrity failures", () => {
  const failed = operation({state: "failed", status_reason: "model artifact digest mismatch", result: {retryable: false}});
  render(<LibraryRunSwitchProgress api={{getRecipeRunSwitchOperation: vi.fn(), retryRecipeRunSwitch: vi.fn()}} nodeNames={{[nodeA]: "Spark One"}} onChange={vi.fn()} operation={failed} title="Qwen Chat"/>);
  expect(screen.queryByRole("button", {name: "Retry run"})).not.toBeInTheDocument();
});

test("one Run action previews and applies the exact model and selected Spark group", async () => {
  const preview: RunSwitchPlan = {
    schema_version: 2,
    generated_at: "2026-09-04T12:00:00Z",
    action: "run",
    model_version_sha256: fullLibraryDetail.model_documents[0]!.selection.model.content_sha256,
    recipe_revision_id: fullLibraryDetail.recipe.recipe_revision_id,
    recipe_content_sha256: recipeRevisionId,
    alias: "qwen-chat",
    run_id: null,
    spark_group: {nodes: [{node_id: nodeA, rank: 0, role: "leader", endpoint_owner: true}]},
    mapping: null,
    installation_id: null,
    installation_state: null,
    recipe_build_id: null,
    image_digest: null,
    start_plan_digest: null,
    model_capabilities: [],
    recipe_capabilities: [],
    freshness: [],
    fit_current: fit,
    fit_after_stop: null,
    fit,
    storage,
    runtime_storage: runtimeStorage,
    build,
    preparation: null,
    conflicts: [],
    stops: [],
    reclaimed_bytes: 0,
    phases: [{index: 0, kind: "start", state: "planned", node_ids: [nodeA], detail: "Start"}],
    allowed: true,
    blockers: [],
    warnings: [],
    invocation: {origin: "web.library"},
    plan_digest: digest,
    stop_before_prepare: false,
    stop_before_transfer: false,
  };
  const started = operation({state: "queued", plan_digest: digest, request_key: "33333333-3333-4333-8333-333333333333"});
  const previewRecipeRunSwitch = vi.fn(async () => preview);
  const applyRecipeRunSwitch = vi.fn(async () => started);
  const detail = {
    ...fullLibraryDetail,
    recipe: {...fullLibraryDetail.recipe, recipe_revision_id: recipeRevisionId},
    placement: [{
      topology_name: "solo",
      node_count: 1,
      candidate_node_ids: [nodeA],
      recommendations: [{
        ...sourceGroup,
        node_ids: [nodeA],
        nodes: [{...sourceGroup.nodes[0]!, node_id: nodeA, rank: 0, endpoint_owner: true}],
        preview_targets: [{kind: "run" as const, input: {installation_id: "installation-chat"}}],
      }],
      rejected_groups: [],
      rejected_nodes: [],
      evaluated_group_count: 1,
      evidence_counts: {builds: 0, mappings: 0, mapping_members: 0, installations: 0, installation_members: 0, runs: 0, run_members: 0, truncated_collections: []},
      limits: {},
      reasons: [],
      rejected_evidence_truncated: false,
      search_complete: true,
    }],
  };
  const runApi = {previewRecipeRunSwitch, applyRecipeRunSwitch, getRecipeRunSwitchOperation: vi.fn(async () => started), retryRecipeRunSwitch: vi.fn()};
  render(<LibraryNodeNamesProvider names={{[nodeA]: "Spark One"}}><LibraryRecipeAuthority api={runApi as never} detail={detail as unknown as LibraryRecipeDetail} snapshot={librarySnapshot}/></LibraryNodeNamesProvider>);
  await act(async () => {
    fireEvent.click(screen.getAllByRole("button", {name: "Run"})[0]!);
    await Promise.resolve();
  });
  expect(previewRecipeRunSwitch).toHaveBeenCalledWith(expect.objectContaining({schema_version: 2, model_version_sha256: fullLibraryDetail.model_documents[0]!.selection.model.content_sha256, recipe_revision_id: recipeRevisionId, action: "run", spark_group: {nodes: [{node_id: nodeA, rank: 0, role: "leader", endpoint_owner: true}]}}));
  expect(screen.queryByRole("button", {name: "Review Load"})).not.toBeInTheDocument();
  expect(await screen.findByText("Copying model to Spark One")).toBeVisible();
  expect(applyRecipeRunSwitch).toHaveBeenCalledWith(expect.objectContaining({plan_digest: digest, request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)}));
});
