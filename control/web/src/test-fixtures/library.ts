import type {LibraryRecipeDetail, LibraryRecipeSummary, LibrarySnapshot} from "../api/types";

const revision = {
  content_sha256: "a".repeat(64),
  created_at: "2026-08-15T10:00:00Z",
  id: "revision-chat",
  lifecycle: "resolved" as const,
  revision_number: 3,
  schema_version: 1 as const,
};

export function libraryRecipeSummary(input: Partial<LibraryRecipeSummary> & Pick<LibraryRecipeSummary, "recipe_id" | "slug" | "title">): LibraryRecipeSummary {
  return {
    capabilities: ["openai.chat"],
    description: `${input.title} description`,
    installation_returned_count: 0,
    installation_total_count: 0,
    installations: [],
    installations_truncated: false,
    topology_name: "pair",
    reasons: [],
    run_returned_count: 0,
    run_total_count: 0,
    runs: [],
    runs_truncated: false,
    selected_revision: revision,
    source_kind: "local",
    ...input,
  };
}

export const chatRecipe = libraryRecipeSummary({recipe_id: "recipe-chat", slug: "qwen-chat", title: "Qwen Chat"});
export const codeRecipe = libraryRecipeSummary({recipe_id: "recipe-code", slug: "qwen-code", title: "Qwen Code", capabilities: ["openai.completions"]});
export const unlinkedRecipe = libraryRecipeSummary({recipe_id: "recipe-unlinked", slug: "custom", title: "Custom Runtime", selected_revision: null, topology_name: null, capabilities: []});

export const librarySnapshot: LibrarySnapshot = {
  schema_version: 1,
  generated_at: "2026-08-15T12:00:00Z",
  freshness_policy: {inventory_fresh_seconds: 300, telemetry_live_seconds: 6, telemetry_delayed_seconds: 20},
  models: [{model: {kind: "model-version", publisher: "qwen", slug: "3", content_sha256: "e".repeat(64)}, page_local: true, recipes: [chatRecipe, codeRecipe]}],
  unlinked_recipes: [unlinkedRecipe],
  next_cursor: null,
};

export const minimalLibraryDetail: LibraryRecipeDetail = {
  schema_version: 1,
  generated_at: "2026-08-15T12:00:00Z",
  recipe: {recipe_id: chatRecipe.recipe_id, slug: chatRecipe.slug, title: chatRecipe.title, description: chatRecipe.description, source_kind: "local"},
  selected_revision: revision,
  visual_recipe: null,
  topology: null,
  operational_state: {builds: [], mappings: [], installations: [], runs: []},
  placement: [],
  reasons: [],
};

const GIB = 1024 ** 3;
const limits = {
  artifact_evidence_per_node_limit: 512 as const,
  candidate_node_limit: 32 as const,
  examined_group_limit: 512 as const,
  operational_member_evidence_limit: 16384 as const,
  operational_row_evidence_limit: 512 as const,
  recommendation_limit: 16 as const,
  rejected_group_evidence_limit: 16 as const,
  rejected_node_evidence_limit: 32 as const,
};

function placementNode(nodeId: string, rank: number, role: string, inventoryAge: number, telemetryAge: number) {
  return {
    artifact_reuse_bytes: 20 * GIB,
    disk_free_after_bytes: 135 * GIB,
    disk_free_bytes: 200 * GIB,
    disk_required_bytes: 60 * GIB,
    disk_reserved_bytes: 5 * GIB,
    endpoint_owner: rank === 0,
    fabric_address: `fabric://${nodeId}`,
    fabric_bandwidth_mbps: 25_000,
    inventory_age_seconds: inventoryAge,
    inventory_observed_at: "2026-08-15T11:59:50Z",
    memory_available_bytes: 100 * GIB,
    memory_free_after_bytes: 36 * GIB,
    memory_kind: "unified" as const,
    memory_required_bytes: 60 * GIB,
    memory_reserved_bytes: 4 * GIB,
    node_id: nodeId,
    rank,
    role,
    telemetry_age_seconds: telemetryAge,
    telemetry_observed_at: "2026-08-15T11:59:58Z",
  };
}

const selectedGroup = {
  eligible: true,
  group_complete: true as const,
  install_state: "complete" as const,
  installation_ids: ["installation-chat"],
  load_state: "not_loaded" as const,
  mapping_id: "mapping-chat",
  node_ids: ["node-alpha", "node-beta"],
  nodes: [placementNode("node-alpha", 0, "leader", 10, 2), placementNode("node-beta", 1, "worker", 15, 12)],
  preview_targets: [{kind: "run" as const, input: {installation_id: "installation-chat"}}],
  topology_name: "pair",
  ranking_scope: "bounded-advisory" as const,
  reasons: [{code: "placement.artifact_reuse", detail: "40.0 GiB of exact artifacts can be reused.", severity: "info" as const}],
  recipe_build_id: "build-chat",
  recipe_revision_id: "revision-chat",
  run_ids: [],
  score: {active_run_count: 0, artifact_reuse_bytes: 40 * GIB, exact_install_complete: true, exact_install_partial: false, maximum_telemetry_age_seconds: 12, minimum_disk_headroom_bytes: 135 * GIB, minimum_memory_headroom_bytes: 36 * GIB},
};

const rejectedGroup = {
  ...selectedGroup,
  eligible: false,
  install_state: "partial" as const,
  installation_ids: ["installation-partial"],
  load_state: "unknown" as const,
  mapping_id: null,
  node_ids: ["node-gamma", "node-delta"],
  nodes: [placementNode("node-gamma", 0, "leader", 350, 30), placementNode("node-delta", 1, "worker", 360, 45)],
  preview_targets: [],
  reasons: [{code: "inventory.stale", detail: "Admission inventory is stale for this complete group.", severity: "error" as const}, {code: "telemetry.stale", detail: "Live capacity evidence is stale for this complete group.", severity: "error" as const}],
  run_ids: ["run-degraded"],
  score: {...selectedGroup.score, exact_install_complete: false, exact_install_partial: true, maximum_telemetry_age_seconds: 45},
};

export const fullLibraryDetail: LibraryRecipeDetail = {
  schema_version: 1,
  generated_at: "2026-08-15T12:00:00Z",
  recipe: {recipe_id: chatRecipe.recipe_id, slug: chatRecipe.slug, title: chatRecipe.title, description: "Fast distributed chat model.", source_kind: "local"},
  selected_revision: revision,
  visual_recipe: {
    schema_version: 1,
    identity: {publisher: "local", slug: "qwen-chat"},
    metadata: {title: "Qwen Chat", description: "Fast distributed chat model.", tags: ["chat", "multilingual"]},
    model: {kind: "model-version", publisher: "qwen", slug: "qwen3", content_sha256: "e".repeat(64)},
    execution: {harness: {kind: "execution-harness", publisher: "vonk-forge", slug: "vllm-openai", content_sha256: "f".repeat(64)}, patch_bundle: null},
    model_license: null,
    parameters: [],
    build: {context: {sha256: "b".repeat(64), expected_bytes: 4096, media_type: "application/vnd.vonk-forge.source-bundle.v1+tar"}, dockerfile: "Dockerfile", target: null, capabilities: [], options: {additional_contexts: [], annotations: [], environment: [], format: "oci", identity_label: true, ignorefile: null, jobs: 1, labels: [], layer_compression: "disabled", layer_labels: [], layers: true, no_hostname: false, no_hosts: false, omit_history: false, os_features: [], os_version: null, shm_bytes: 67108864, skip_unused_stages: true, squash: "none", timestamp: null, unset_environment: [], unset_labels: []}, cpu_cores: 8, download_bytes: 60 * GIB, memory_bytes: 8 * GIB, network_hosts: [], network_mode: "none", platform: "linux/arm64", processes: 4096, temporary_bytes: 12 * GIB, timeout_seconds: 3600},
    artifacts: [{id: "weights", kind: "huggingface.snapshot", repository: "Qwen/Qwen3", revision: "c".repeat(40), include_paths: ["config.json", "model/model.safetensors"], download_bytes: 50 * GIB, installed_bytes: 52 * GIB, roles: ["leader", "worker"]}],
    runtime: {distribution: {kind: "runtime-distribution", publisher: "vonk-forge", slug: "python-312-cuda", content_sha256: "1".repeat(64)}, entrypoint: ["vllm", "serve", "/models"], lifecycle_pre_start_count: 1, lifecycle_post_stop_count: 1, stop_timeout_seconds: 30},
    interfaces: [{adapter: "openai", port: 8000, health_path: "/v1/models", model_aliases: ["qwen-chat"]}],
    validation: {benchmark_count: 2, checks: ["container.started", "inference.completed"]},
    provenance: {source_kind: "local", source_reference: "source-bundle", attribution: ["Qwen Team"]},
  },
  topology: {
    name: "pair", mode: "tensor_parallel", node_count: 2,
    parallelism: {tensor: 2, pipeline: 1, data: 1, backend: "nccl"}, fabric: {connectivity: "connected", minimum_bandwidth_mbps: 10_000}, start_order: ["leader", "worker"], stop_order: ["worker", "leader"],
    roles: [
      {name: "leader", count: 1, endpoint_owner: true, artifacts: ["weights"], disk: {image_bytes: 8 * GIB, artifact_bytes: 52 * GIB, staging_bytes: 4 * GIB, cache_bytes: 1 * GIB, rollback_bytes: 2 * GIB, safety_margin_bytes: 3 * GIB}, memory: {kind: "unified", startup_peak_bytes: 72 * GIB, steady_state_bytes: 64 * GIB, runtime_growth_bytes: 4 * GIB, system_reserve_bytes: 8 * GIB}},
      {name: "worker", count: 1, endpoint_owner: false, artifacts: ["weights"], disk: {image_bytes: 8 * GIB, artifact_bytes: 52 * GIB, staging_bytes: 4 * GIB, cache_bytes: 1 * GIB, rollback_bytes: 2 * GIB, safety_margin_bytes: 3 * GIB}, memory: {kind: "unified", startup_peak_bytes: 72 * GIB, steady_state_bytes: 64 * GIB, runtime_growth_bytes: 4 * GIB, system_reserve_bytes: 8 * GIB}},
    ],
  },
  operational_state: {
    builds: [{recipe_build_id: "build-chat", recipe_revision_id: "revision-chat", state: "succeeded", image_digest: `sha256:${"d".repeat(64)}`, image_bytes: 8 * GIB}],
    mappings: [{mapping_id: "mapping-chat", recipe_revision_id: "revision-chat", topology_name: "pair", generation: 4, state: "ready", nodes: [{node_id: "node-alpha", rank: 0, role: "leader", endpoint_owner: true}, {node_id: "node-beta", rank: 1, role: "worker", endpoint_owner: false}]}],
    installations: [{installation_id: "installation-chat", mapping_id: "mapping-chat", recipe_build_id: "build-chat", recipe_revision_id: "revision-chat", node_ids: ["node-alpha", "node-beta"], state: "installed"}],
    runs: [],
  },
  placement: [{
    topology_name: "pair", node_count: 2, candidate_node_ids: ["node-alpha", "node-beta", "node-gamma", "node-delta"], evaluated_group_count: 512,
    evidence_counts: {builds: 1, mappings: 1, mapping_members: 2, installations: 2, installation_members: 4, runs: 1, run_members: 2, truncated_collections: []},
    limits, recommendations: [selectedGroup], rejected_groups: [rejectedGroup], rejected_nodes: [{node_id: "node-epsilon", reasons: [{code: "inventory.missing", detail: "Admission inventory has not been reported.", severity: "error"}]}], rejected_evidence_truncated: true, search_complete: false,
    reasons: [{code: "placement.group_search_truncated", detail: "The bounded group search stopped after 512 complete groups.", severity: "warning"}],
  }],
  reasons: [{code: "recipe.visual_projection", detail: "Visual recipe fields are bounded to the selected immutable revision.", severity: "info"}],
};
