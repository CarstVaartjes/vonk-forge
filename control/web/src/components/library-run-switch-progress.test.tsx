import {act, fireEvent, render, screen} from "@testing-library/react";
import type {LibraryRecipeDetail, RunSwitchOperation, RunSwitchPlan} from "../api/types";
import {LibraryRecipeAuthority} from "./library-recipe-detail";
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

const canonicalDetail = {
  schema_version: 2,
  generated_at: "2026-09-04T12:00:00Z",
  recipe: {recipe_id: "recipe-chat", recipe_revision_id: recipeRevisionId, publisher: "local", slug: "qwen-chat", title: "Qwen Chat", description: "Fast distributed chat model.", content_sha256: recipeRevisionId},
  definition: {schema_version: 2, kind: "recipe", identity: {publisher: "local", slug: "qwen-chat"}, metadata: {title: "Qwen Chat", description: "Fast distributed chat model.", tags: []}, models: [], execution: {mode: "image", image: {repository: "example/qwen", digest: `sha256:${"b".repeat(64)}`, platform: "linux/arm64"}}, runtime: {engine: "vllm", entrypoint: ["serve"], arguments: [], environment: [], lifecycle: {pre_start: [], post_stop: [], stop_timeout_seconds: 30}}, settings: {kind: "chat"}, interfaces: [{adapter: "openai", model_aliases: ["qwen-chat"]}], validation: {benchmarks: [], serving: {interface: "openai", checks: []}}, provenance: {source_kind: "local", source_reference: null, attribution: []}, release: {version: "1", released_at: "2026-09-04T12:00:00Z", history: []}, topology: {name: "solo", mode: "single", node_count: 1, fabric: {connectivity: "none", minimum_bandwidth_mbps: 0}, parallelism: {backend: "local", world_size: 1, tensor: 1, pipeline: 1, data: 1}, roles: [{name: "leader", count: 1, endpoint_owner: true, resources: {disk: {image_bytes: 1, artifact_bytes: 1, staging_bytes: 1, cache_bytes: 0, rollback_bytes: 0, safety_margin_bytes: 1}, memory: {kind: "host", startup_peak_bytes: 1, steady_state_bytes: 1, runtime_growth_bytes: 0, system_reserve_bytes: 1}}}], start_order: ["leader"], stop_order: ["leader"]}},
  topology: {name: "solo", mode: "single", node_count: 1, fabric: {connectivity: "none", minimum_bandwidth_mbps: 0}, parallelism: {backend: "local", world_size: 1, tensor: 1, pipeline: 1, data: 1}, roles: [{name: "leader", count: 1, endpoint_owner: true, resources: {disk: {image_bytes: 1, artifact_bytes: 1, staging_bytes: 1, cache_bytes: 0, rollback_bytes: 0, safety_margin_bytes: 1}, memory: {kind: "host", startup_peak_bytes: 1, steady_state_bytes: 1, runtime_growth_bytes: 0, system_reserve_bytes: 1}}}], start_order: ["leader"], stop_order: ["leader"]},
  operational_state: {builds: [], mappings: [], installations: [], runs: []},
  placement: [{topology_name: "solo", node_count: 1, candidate_node_ids: [nodeA], recommendations: [sourceGroup], rejected_groups: [], rejected_nodes: [], evaluated_group_count: 1, evidence_counts: {builds: 0, mappings: 0, mapping_members: 0, installations: 0, installation_members: 0, runs: 0, run_members: 0, truncated_collections: []}, limits: {}, reasons: [], rejected_evidence_truncated: false, search_complete: true}],
  model_documents: [{model_document: {identity: {publisher: "local", slug: "qwen-model", variant: "default", version: "1", model: {title: "Qwen Model"}}, files: []}, selection: {id: "selection-qwen", model: {kind: "model", publisher: "local", slug: "qwen-model", content_sha256: digest}, files: []}}],
  model_capabilities: {schema_version: 2, state: "unknown", facts: [], provenance: null, reasons: []},
  recipe_capabilities: {schema_version: 2, state: "unknown", facts: [], provenance: null, reasons: []},
  reasons: [],
} as unknown as LibraryRecipeDetail;

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
  render(<LibraryRunSwitchProgress api={{getRecipeRunSwitchOperation: getOperation}} nodeNames={{[nodeA]: "Spark One"}} onChange={vi.fn()} onRetry={vi.fn()} operation={operation()} title="Qwen Chat"/>);
  expect(screen.getByRole("progressbar", {name: "Run progress"})).not.toHaveAttribute("aria-valuenow");
  expect(screen.getAllByText("Total bytes unavailable").length).toBeGreaterThan(0);
  await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
  expect(getOperation).toHaveBeenCalledWith(operation().operation_id, expect.any(AbortSignal));
  vi.useRealTimers();
});

test("one Run action previews and applies the exact model and selected Spark group", async () => {
  const preview: RunSwitchPlan = {
    schema_version: 2,
    generated_at: "2026-09-04T12:00:00Z",
    action: "run",
    model_version_sha256: digest,
    recipe_revision_id: recipeRevisionId,
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
    ...canonicalDetail,
    placement: [{
      ...canonicalDetail.placement[0]!,
      recommendations: [{
        ...sourceGroup,
        node_ids: [nodeA],
        nodes: [{...sourceGroup.nodes[0]!, node_id: nodeA, rank: 0, endpoint_owner: true}],
        preview_targets: [{kind: "run" as const, input: {installation_id: "installation-chat"}}],
      }],
      rejected_groups: [],
    }],
  };
  render(<LibraryRecipeAuthority api={{previewRecipeRunSwitch, applyRecipeRunSwitch, getRecipeRunSwitchOperation: vi.fn(async () => started)} as never} detail={detail as unknown as LibraryRecipeDetail} snapshot={{freshness_policy: {inventory_fresh_seconds: 300, telemetry_live_seconds: 6, telemetry_delayed_seconds: 20}} as never}/>);
  await act(async () => {
    fireEvent.click(screen.getAllByRole("button", {name: "Run"})[0]!);
    await Promise.resolve();
  });
  expect(previewRecipeRunSwitch).toHaveBeenCalledWith(expect.objectContaining({schema_version: 2, model_version_sha256: digest, recipe_revision_id: recipeRevisionId, action: "run", spark_group: {nodes: [{node_id: nodeA, rank: 0, role: "leader", endpoint_owner: true}]}}));
  expect(screen.queryByRole("button", {name: "Review Load"})).not.toBeInTheDocument();
  expect(await screen.findByText("Copying model to Spark node")).toBeVisible();
  expect(applyRecipeRunSwitch).toHaveBeenCalledWith(expect.objectContaining({plan_digest: digest, request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)}));
});
