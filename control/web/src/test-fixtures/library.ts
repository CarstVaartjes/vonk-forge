import type {LibraryModel, LibraryRecipeDetail, LibraryRecipeSummary, LibrarySnapshot} from "../api/types";

const digest = (seed: string): string => seed.repeat(64).slice(0, 64);
const revisionId = (index: number): string => `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`;

function modelDocument(index: number): LibraryModel["model_document"] {
  const publisher = index % 2 === 0 ? "qwen" : "zai-org";
  const slug = index % 2 === 0 ? `qwen-${index + 3}` : `glm-${index + 5}`;
  const title = index % 2 === 0 ? `Qwen ${index + 3}` : `GLM ${index + 5}`;
  return {
    schema_version: 2, kind: "model",
    identity: {publisher, slug, version: "1.0", variant: "bf16", model: {publisher, slug, title, architecture: "transformer"}, family: {publisher, slug: slug.split("-")[0]!, title}},
    metadata: {description: `${title} exact model manifest`, tags: [index % 2 === 0 ? "chat" : "reasoning"]},
    files: [
      {id: "weights", path: "model.safetensors", roles: ["weights"], sha256: digest(String(index + 1)), size_bytes: 6_000_000_000 + index * 10_000_000},
      {id: "config", path: "config.json", roles: ["config"], sha256: digest(String(index + 2)), size_bytes: 16_384},
    ],
    format: {container: "safetensors", precision: "BF16", quantization: "none"},
    capabilities: {chat: true, reasoning: index % 2 === 1, vision: index % 5 === 0},
    modalities: ["text"], parameters: {active: 7_000_000_000, total: 7_000_000_000},
    access: {gated: false, credentials: []}, license: {spdx: "Apache-2.0", url: "https://www.apache.org/licenses/LICENSE-2.0", attribution: [], operator_acceptance_required: false},
    limits: {context_tokens: 32_768, frames: null, resolution_pixels: null, sample_rate_hz: null},
    lineage: {derivation: "", publisher, relation: "official", source_model: {kind: "model", publisher, slug}},
    provenance: {attribution: [publisher], evidence_digest: digest("e"), source_revision: digest("r"), source_url: `https://huggingface.co/${publisher}/${slug}`},
    source: {repository: `https://huggingface.co/${publisher}/${slug}`, revision: digest("s")}, dependencies: [], supersedes: null,
  } as unknown as LibraryModel["model_document"];
}

function model(index: number, recipes: LibraryRecipeSummary[]): LibraryModel {
  const document = modelDocument(index);
  return {
    page_local: true, model: {...document.identity, kind: "model", content_sha256: digest(`m${index}`)}, model_document: document,
    model_capabilities: {schema_version: 2, state: "declared", facts: [{capability: "chat", support: "supported", evidence_status: "declared", evidence_digest: digest("c"), provenance: {source_kind: "model", publisher: document.identity.publisher, slug: document.identity.slug, content_sha256: digest(`m${index}`), evidence_digest: digest("c"), path: "capabilities.chat"}}]}, recipes,
  } as LibraryModel;
}

function recipe(index: number): LibraryRecipeSummary {
  const id = `recipe-${index + 1}`;
  const title = index === 0 ? "Qwen Chat" : index === 1 ? "Qwen Shared Vision" : `Recipe ${index + 1}`;
  const definition = {
    schema_version: 2, kind: "recipe", identity: {publisher: "vonk-forge", slug: id}, metadata: {title, description: `${title} exact execution contract`, tags: ["chat"]},
    models: (index === 0 ? [0, 1] : [index]).map((modelIndex, modelPosition) => ({id: modelPosition === 0 ? "primary" : `auxiliary-${modelPosition}`, model: {kind: "model", publisher: modelIndex % 2 === 0 ? "qwen" : "zai-org", slug: modelIndex % 2 === 0 ? `qwen-${modelIndex + 3}` : `glm-${modelIndex + 5}`, content_sha256: digest(`m${modelIndex}`)}, files: [{id: "weights", file_id: "weights", roles: ["weights"], mount: {read_only: true, target: `/models/${modelPosition}`}}]})),
    execution: {mode: "image", image: {repository: "ghcr.io/vonk-forge/vllm", digest: `sha256:${digest("i")}`, platform: "linux/arm64"}}, runtime: {engine: "vllm", entrypoint: ["vllm", "serve"], arguments: [], environment: [], lifecycle: {pre_start: [], post_stop: [], stop_timeout_seconds: 30}},
    interfaces: [{adapter: "openai", port: 8000, health_path: "/health", model_aliases: [title.toLowerCase().replaceAll(" ", "-")]}],
    topology: {name: index % 3 === 0 ? "dual" : "single", mode: index % 3 === 0 ? "distributed" : "single", node_count: index % 3 === 0 ? 2 : 1, parallelism: {backend: "native-mp", data: 1, pipeline: 1, tensor: index % 3 === 0 ? 2 : 1, world_size: index % 3 === 0 ? 2 : 1}, fabric: {connectivity: index % 3 === 0 ? "full_mesh" : "none", minimum_bandwidth_mbps: index % 3 === 0 ? 10_000 : 0}, roles: [{name: "worker", count: index % 3 === 0 ? 2 : 1, endpoint_owner: true, resources: {disk: {artifact_bytes: 0, cache_bytes: 0, image_bytes: 1_000_000, rollback_bytes: 0, safety_margin_bytes: 1_000_000, staging_bytes: 0}, memory: {kind: "unified", runtime_growth_bytes: 1_000_000, startup_peak_bytes: 4_000_000, steady_state_bytes: 3_000_000, system_reserve_bytes: 1_000_000}}}], start_order: ["worker"], stop_order: ["worker"]},
    settings: {kind: "generation", context_tokens: {value: 32_768, change_effect: "restart"}}, release: {version: "1.0.0", released_at: "2026-09-01", history: []}, provenance: {source_kind: "local", source_reference: null, attribution: ["Vonk Forge"]}, validation: {benchmarks: [], serving: {interface: "openai", checks: []}},
  } as LibraryRecipeSummary["recipe_document"];
  return {capabilities: ["chat"], content_sha256: digest(`r${index}`), description: definition.metadata.description, installation_returned_count: 0, installation_total_count: 0, installations: [], installations_truncated: false, publisher: "vonk-forge", reasons: [], recipe_capabilities: {schema_version: 2, state: "declared", facts: []}, recipe_document: definition, recipe_id: id, recipe_revision_id: revisionId(index), run_returned_count: 0, run_total_count: 0, runs: [], runs_truncated: false, slug: id, title, topology_name: definition.topology.name} as LibraryRecipeSummary;
}

export function libraryRecipeSummary(input: Partial<LibraryRecipeSummary> & Pick<LibraryRecipeSummary, "recipe_id" | "slug" | "title">): LibraryRecipeSummary {
  const base = recipe(0);
  return {...base, ...input, recipe_document: {...base.recipe_document, metadata: {...base.recipe_document.metadata, title: input.title ?? base.title, description: input.description ?? base.description}, identity: {...base.recipe_document.identity, slug: input.slug}}};
}

const recipes = Array.from({length: 85}, (_, index) => recipe(index));
const models = Array.from({length: 92}, (_, index) => {
  if (index >= 79) return model(index, []);
  const assigned = recipes.filter((_, recipeIndex) => recipeIndex % 79 === index);
  if (index === 1) assigned.push(recipes[0]!);
  return model(index, assigned);
});

export const librarySnapshot: LibrarySnapshot = {schema_version: 2, generated_at: "2026-09-06T00:00:00Z", freshness_policy: {inventory_fresh_seconds: 300, telemetry_delayed_seconds: 20, telemetry_live_seconds: 6}, next_cursor: null, models, unlinked_recipes: []};
export const chatRecipe = recipes[0]!;
export const codeRecipe = recipes[1]!;
export const unlinkedRecipe = libraryRecipeSummary({recipe_id: "recipe-unlinked", recipe_revision_id: revisionId(10_000), slug: "unlinked", title: "Unlinked recipe"} as Partial<LibraryRecipeSummary> & Pick<LibraryRecipeSummary, "recipe_id" | "slug" | "title">);

export const minimalLibraryDetail: LibraryRecipeDetail = {schema_version: 2, generated_at: "2026-09-06T00:00:00Z", definition: recipes[0]!.recipe_document, recipe: {...recipes[0]!, recipe_revision_id: revisionId(0)}, model_documents: [{model_document: models[0]!.model_document, selection: recipes[0]!.recipe_document.models[0]!}, {model_document: models[1]!.model_document, selection: recipes[0]!.recipe_document.models[1]!}], model_capabilities: models[0]!.model_capabilities, recipe_capabilities: recipes[0]!.recipe_capabilities, operational_state: {builds: [], installations: [], mappings: [], runs: []}, placement: [], reasons: [], topology: recipes[0]!.recipe_document.topology};
export const fullLibraryDetail = minimalLibraryDetail;
