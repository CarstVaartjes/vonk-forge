import presetData from "../pages/custom-recipe-presets.json";

export type Scalar = string | number | boolean;
export type PresetName = "custom" | "vllm" | "diffusers";
export type InterfaceAdapter = "openai" | "image-job" | "audio-job" | "video-job" | "mesh-job" | "artifact-job";
export type TopologyMode = "single" | "distributed" | "tensor_parallel" | "pipeline_parallel" | "data_parallel" | "hybrid" | "ray" | "mpi";

export type CanonicalReference = {
  kind: "model-version" | "execution-harness" | "runtime-distribution" | "patch-bundle";
  publisher: string;
  slug: string;
  content_sha256: string;
};

export type CanonicalArtifact = {
  id: string;
  kind: "huggingface.snapshot" | "http.file" | "oci.artifact";
  repository: string;
  revision: string;
  download_bytes: number;
  installed_bytes: number;
  mount: {target: string; read_only: boolean};
  roles: string[];
};

export type CanonicalInterface = {
  adapter: InterfaceAdapter;
  port?: number;
  model_aliases?: string[];
  health_path?: string;
  path?: string;
  input?: {path: "/inputs"; required: boolean; media_types: string[]; max_bytes: number};
};

type RoleResources = {
  disk: {image_bytes: number; artifact_bytes: number; staging_bytes: number; cache_bytes: number; rollback_bytes: number; safety_margin_bytes: number};
  memory: {kind: "unified" | "host" | "accelerator"; startup_peak_bytes: number; steady_state_bytes: number; runtime_growth_bytes: number; system_reserve_bytes: number};
};

export type CanonicalRecipeDocument = {
  schema_version: 1;
  identity: {publisher: string; slug: string};
  metadata: {title: string; description: string; tags: string[]};
  model: CanonicalReference & {kind: "model-version"};
  dependencies?: Array<CanonicalReference & {kind: "model-version"}>;
  execution: {harness: CanonicalReference & {kind: "execution-harness"}; patch_bundle: (CanonicalReference & {kind: "patch-bundle"}) | null};
  build: {
    context: {sha256: string; expected_bytes: number; media_type: string; path?: string};
    dockerfile: string;
    target?: string;
    platform: string;
    arguments: Array<{name: string; value: Scalar}>;
    network: {mode: "none" | "public"; hosts: string[]};
    resources: {download_bytes: number; temporary_bytes: number; memory_bytes: number; timeout_seconds: number};
  };
  parameters: Array<{name: string; description: string; type: "string" | "integer" | "boolean" | "enum"; default: Scalar; minimum?: number; maximum?: number; allowed_values?: Scalar[]; pattern?: string; change_effect: "rebuild" | "reinstall" | "restart"}>;
  artifacts: CanonicalArtifact[];
  runtime: {
    distribution: CanonicalReference & {kind: "runtime-distribution"};
    entrypoint: string[];
    arguments: Array<{name: string; value?: Scalar; parameter?: string}>;
    environment: Array<{name: string; value?: Scalar; secret?: string}>;
    security: {devices: string[]; capabilities: never[]; host_network: boolean; privileged: false; user: string; mounts: Array<{source: string; target: string; read_only: boolean}>};
    lifecycle: {pre_start: string[][]; post_stop: string[][]; stop_timeout_seconds: number; readiness?: {strategy: "endpoint-owner" | "endpoint-owner-after-all-ranks"; path: string; timeout_seconds: number}; failure?: {rank_loss: "not-applicable" | "withdraw-endpoint"; recovery: "restart-entrypoint" | "restart-worker-then-entrypoint"}};
  };
  topology: {
    name: string;
    mode: TopologyMode;
    node_count: number;
    roles: Array<{name: string; count: number; endpoint_owner: boolean; artifacts: string[]; resources: RoleResources}>;
    parallelism: {world_size: number; tensor: number; pipeline: number; data: number; backend: string};
    fabric: {connectivity: "none" | "connected" | "full_mesh" | "switch"; minimum_bandwidth_mbps: number};
    start_order: string[];
    stop_order: string[];
  };
  interfaces: CanonicalInterface[];
  validation: {validators: Array<{interface: InterfaceAdapter; checks: string[]}>; benchmarks: Array<{name: string; framework: string; configuration: Record<string, Scalar>}>};
  provenance: {source_kind: "local" | "workload_run" | "global" | "fork"; source_reference: string | null; attribution: string[]};
};

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function mergePatch(base: unknown, patch: unknown): unknown {
  if (!isObject(base) || !isObject(patch)) return structuredClone(patch);
  const result: JsonObject = structuredClone(base);
  for (const [key, value] of Object.entries(patch)) result[key] = key in result ? mergePatch(result[key], value) : structuredClone(value);
  return result;
}

export function createCanonicalRecipeDocument(preset: PresetName = "custom"): CanonicalRecipeDocument {
  return mergePatch(presetData.base, presetData.presets[preset]) as CanonicalRecipeDocument;
}

export type CanonicalRecipeParseResult =
  | {ok: true; document: CanonicalRecipeDocument}
  | {ok: false; error: string};

class ShapeError extends Error {}

function objectAt(value: unknown, path: string): JsonObject {
  if (!isObject(value)) throw new ShapeError(`${path} must be an object.`);
  return value;
}

function arrayAt(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new ShapeError(`${path} must be an array.`);
  return value;
}

function requireObject(parent: JsonObject, key: string, path = `$`): JsonObject {
  return objectAt(parent[key], `${path}.${key}`);
}

function requireArray(parent: JsonObject, key: string, path = `$`): unknown[] {
  return arrayAt(parent[key], `${path}.${key}`);
}

function validateShape(value: unknown): CanonicalRecipeDocument {
  const root = objectAt(value, "$");
  if (root.schema_version !== 1) throw new ShapeError("$.schema_version must equal 1.");
  requireObject(root, "identity"); requireObject(root, "metadata"); requireObject(root, "model");
  const execution = requireObject(root, "execution"); requireObject(execution, "harness", "$.execution");
  const build = requireObject(root, "build"); requireObject(build, "context", "$.build"); requireObject(build, "network", "$.build"); requireObject(build, "resources", "$.build"); requireArray(build, "arguments", "$.build");
  requireArray(root, "parameters");
  requireArray(root, "artifacts").forEach((item, index) => requireObject(objectAt(item, `$.artifacts[${index}]`), "mount", `$.artifacts[${index}]`));
  const runtime = requireObject(root, "runtime"); requireObject(runtime, "distribution", "$.runtime"); requireArray(runtime, "entrypoint", "$.runtime"); requireArray(runtime, "arguments", "$.runtime"); requireArray(runtime, "environment", "$.runtime"); requireObject(runtime, "security", "$.runtime"); requireObject(runtime, "lifecycle", "$.runtime");
  const topology = requireObject(root, "topology"); requireArray(topology, "roles", "$.topology").forEach((item, index) => { const role = objectAt(item, `$.topology.roles[${index}]`); const resources = requireObject(role, "resources", `$.topology.roles[${index}]`); requireObject(resources, "disk", `$.topology.roles[${index}].resources`); requireObject(resources, "memory", `$.topology.roles[${index}].resources`); }); requireObject(topology, "parallelism", "$.topology"); requireObject(topology, "fabric", "$.topology"); requireArray(topology, "start_order", "$.topology"); requireArray(topology, "stop_order", "$.topology");
  requireArray(root, "interfaces");
  const validation = requireObject(root, "validation"); requireArray(validation, "validators", "$.validation"); requireArray(validation, "benchmarks", "$.validation");
  requireObject(root, "provenance");
  return value as CanonicalRecipeDocument;
}

function syntaxLocation(text: string, error: SyntaxError): string {
  const position = Number(/position (\d+)/.exec(error.message)?.[1] ?? text.length);
  const before = text.slice(0, position); const lines = before.split("\n");
  return `Invalid JSON at line ${lines.length}, column ${lines.at(-1)!.length + 1}.`;
}

export function parseCanonicalRecipeDocument(text: string): CanonicalRecipeParseResult {
  try { return {ok: true, document: validateShape(JSON.parse(text) as unknown)}; }
  catch (value) {
    if (value instanceof ShapeError) return {ok: false, error: value.message};
    if (value instanceof SyntaxError) return {ok: false, error: syntaxLocation(text, value)};
    return {ok: false, error: "Unable to validate the canonical recipe document."};
  }
}
