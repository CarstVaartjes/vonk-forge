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

function stringAt(value: unknown, path: string): string {
  if (typeof value !== "string") throw new ShapeError(`${path} must be a string.`);
  return value;
}

function requireString(parent: JsonObject, key: string, path = "$"): string {
  return stringAt(parent[key], `${path}.${key}`);
}

function integerAt(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value)) throw new ShapeError(`${path} must be an integer.`);
  return value as number;
}

function requireInteger(parent: JsonObject, key: string, path = "$"): number {
  return integerAt(parent[key], `${path}.${key}`);
}

function requireBoolean(parent: JsonObject, key: string, path = "$"): boolean {
  if (typeof parent[key] !== "boolean") throw new ShapeError(`${path}.${key} must be a boolean.`);
  return parent[key] as boolean;
}

function scalarAt(value: unknown, path: string): Scalar {
  if (typeof value !== "string" && typeof value !== "boolean" && !Number.isSafeInteger(value)) throw new ShapeError(`${path} must be a string, integer, or boolean.`);
  return value as Scalar;
}

function stringsAt(value: unknown, path: string): string[] {
  return arrayAt(value, path).map((item, index) => stringAt(item, `${path}[${index}]`));
}

function requireStrings(parent: JsonObject, key: string, path = "$"): string[] {
  return stringsAt(parent[key], `${path}.${key}`);
}

function optionalString(parent: JsonObject, key: string, path: string): void {
  if (parent[key] !== undefined) stringAt(parent[key], `${path}.${key}`);
}

function validateReference(value: unknown, path: string): void {
  const reference = objectAt(value, path);
  requireString(reference, "kind", path);
  requireString(reference, "publisher", path);
  requireString(reference, "slug", path);
  requireString(reference, "content_sha256", path);
}

function validateMount(value: unknown, path: string): void {
  const mount = objectAt(value, path);
  requireString(mount, "source", path);
  requireString(mount, "target", path);
  requireBoolean(mount, "read_only", path);
}

function validateShape(value: unknown): CanonicalRecipeDocument {
  const root = objectAt(value, "$");
  if (root.schema_version !== 1) throw new ShapeError("$.schema_version must equal 1.");
  const identity = requireObject(root, "identity");
  requireString(identity, "publisher", "$.identity"); requireString(identity, "slug", "$.identity");
  const metadata = requireObject(root, "metadata");
  requireString(metadata, "title", "$.metadata"); requireString(metadata, "description", "$.metadata"); requireStrings(metadata, "tags", "$.metadata");
  validateReference(root.model, "$.model");
  if (root.dependencies !== undefined) arrayAt(root.dependencies, "$.dependencies").forEach((item, index) => validateReference(item, `$.dependencies[${index}]`));

  const execution = requireObject(root, "execution");
  validateReference(execution.harness, "$.execution.harness");
  if (execution.patch_bundle !== null) validateReference(execution.patch_bundle, "$.execution.patch_bundle");

  const build = requireObject(root, "build");
  const context = requireObject(build, "context", "$.build");
  requireString(context, "sha256", "$.build.context"); requireInteger(context, "expected_bytes", "$.build.context"); requireString(context, "media_type", "$.build.context"); optionalString(context, "path", "$.build.context");
  requireString(build, "dockerfile", "$.build"); optionalString(build, "target", "$.build"); requireString(build, "platform", "$.build");
  requireArray(build, "arguments", "$.build").forEach((item, index) => { const path = `$.build.arguments[${index}]`; const argument = objectAt(item, path); requireString(argument, "name", path); scalarAt(argument.value, `${path}.value`); });
  const network = requireObject(build, "network", "$.build"); requireString(network, "mode", "$.build.network"); requireStrings(network, "hosts", "$.build.network");
  const buildResources = requireObject(build, "resources", "$.build");
  for (const key of ["download_bytes", "temporary_bytes", "memory_bytes", "timeout_seconds"]) requireInteger(buildResources, key, "$.build.resources");

  requireArray(root, "parameters").forEach((item, index) => {
    const path = `$.parameters[${index}]`; const parameter = objectAt(item, path);
    requireString(parameter, "name", path); requireString(parameter, "description", path); requireString(parameter, "type", path); scalarAt(parameter.default, `${path}.default`); requireString(parameter, "change_effect", path);
    if (parameter.minimum !== undefined) integerAt(parameter.minimum, `${path}.minimum`); if (parameter.maximum !== undefined) integerAt(parameter.maximum, `${path}.maximum`); if (parameter.allowed_values !== undefined) arrayAt(parameter.allowed_values, `${path}.allowed_values`).forEach((entry, itemIndex) => scalarAt(entry, `${path}.allowed_values[${itemIndex}]`)); optionalString(parameter, "pattern", path);
  });
  requireArray(root, "artifacts").forEach((item, index) => {
    const path = `$.artifacts[${index}]`; const artifact = objectAt(item, path);
    for (const key of ["id", "kind", "repository", "revision"]) requireString(artifact, key, path);
    requireInteger(artifact, "download_bytes", path); requireInteger(artifact, "installed_bytes", path);
    const mount = requireObject(artifact, "mount", path); requireString(mount, "target", `${path}.mount`); requireBoolean(mount, "read_only", `${path}.mount`); requireStrings(artifact, "roles", path);
  });

  const runtime = requireObject(root, "runtime");
  validateReference(runtime.distribution, "$.runtime.distribution"); requireStrings(runtime, "entrypoint", "$.runtime");
  requireArray(runtime, "arguments", "$.runtime").forEach((item, index) => { const path = `$.runtime.arguments[${index}]`; const argument = objectAt(item, path); requireString(argument, "name", path); if (argument.value !== undefined) scalarAt(argument.value, `${path}.value`); optionalString(argument, "parameter", path); });
  requireArray(runtime, "environment", "$.runtime").forEach((item, index) => { const path = `$.runtime.environment[${index}]`; const environment = objectAt(item, path); requireString(environment, "name", path); if (environment.value !== undefined) scalarAt(environment.value, `${path}.value`); optionalString(environment, "secret", path); });
  const security = requireObject(runtime, "security", "$.runtime"); requireStrings(security, "devices", "$.runtime.security"); requireStrings(security, "capabilities", "$.runtime.security"); requireBoolean(security, "host_network", "$.runtime.security"); requireBoolean(security, "privileged", "$.runtime.security"); requireString(security, "user", "$.runtime.security"); requireArray(security, "mounts", "$.runtime.security").forEach((item, index) => validateMount(item, `$.runtime.security.mounts[${index}]`));
  const lifecycle = requireObject(runtime, "lifecycle", "$.runtime");
  for (const key of ["pre_start", "post_stop"]) requireArray(lifecycle, key, "$.runtime.lifecycle").forEach((command, index) => stringsAt(command, `$.runtime.lifecycle.${key}[${index}]`));
  requireInteger(lifecycle, "stop_timeout_seconds", "$.runtime.lifecycle");
  if (lifecycle.readiness !== undefined) { const readiness = objectAt(lifecycle.readiness, "$.runtime.lifecycle.readiness"); requireString(readiness, "strategy", "$.runtime.lifecycle.readiness"); requireString(readiness, "path", "$.runtime.lifecycle.readiness"); requireInteger(readiness, "timeout_seconds", "$.runtime.lifecycle.readiness"); }
  if (lifecycle.failure !== undefined) { const failure = objectAt(lifecycle.failure, "$.runtime.lifecycle.failure"); requireString(failure, "rank_loss", "$.runtime.lifecycle.failure"); requireString(failure, "recovery", "$.runtime.lifecycle.failure"); }

  const topology = requireObject(root, "topology"); requireString(topology, "name", "$.topology"); requireString(topology, "mode", "$.topology"); requireInteger(topology, "node_count", "$.topology");
  requireArray(topology, "roles", "$.topology").forEach((item, index) => {
    const path = `$.topology.roles[${index}]`; const role = objectAt(item, path); requireString(role, "name", path); requireInteger(role, "count", path); requireBoolean(role, "endpoint_owner", path); requireStrings(role, "artifacts", path);
    const resources = requireObject(role, "resources", path); const disk = requireObject(resources, "disk", `${path}.resources`); for (const key of ["image_bytes", "artifact_bytes", "staging_bytes", "cache_bytes", "rollback_bytes", "safety_margin_bytes"]) requireInteger(disk, key, `${path}.resources.disk`);
    const memory = requireObject(resources, "memory", `${path}.resources`); requireString(memory, "kind", `${path}.resources.memory`); for (const key of ["startup_peak_bytes", "steady_state_bytes", "runtime_growth_bytes", "system_reserve_bytes"]) requireInteger(memory, key, `${path}.resources.memory`);
  });
  const parallelism = requireObject(topology, "parallelism", "$.topology"); for (const key of ["world_size", "tensor", "pipeline", "data"]) requireInteger(parallelism, key, "$.topology.parallelism"); requireString(parallelism, "backend", "$.topology.parallelism");
  const fabric = requireObject(topology, "fabric", "$.topology"); requireString(fabric, "connectivity", "$.topology.fabric"); requireInteger(fabric, "minimum_bandwidth_mbps", "$.topology.fabric"); requireStrings(topology, "start_order", "$.topology"); requireStrings(topology, "stop_order", "$.topology");

  requireArray(root, "interfaces").forEach((item, index) => { const path = `$.interfaces[${index}]`; const itemObject = objectAt(item, path); requireString(itemObject, "adapter", path); if (itemObject.port !== undefined) integerAt(itemObject.port, `${path}.port`); optionalString(itemObject, "health_path", path); optionalString(itemObject, "path", path); if (itemObject.model_aliases !== undefined) stringsAt(itemObject.model_aliases, `${path}.model_aliases`); if (itemObject.input !== undefined) { const input = objectAt(itemObject.input, `${path}.input`); requireString(input, "path", `${path}.input`); requireBoolean(input, "required", `${path}.input`); requireStrings(input, "media_types", `${path}.input`); requireInteger(input, "max_bytes", `${path}.input`); } });
  const validation = requireObject(root, "validation"); requireArray(validation, "validators", "$.validation").forEach((item, index) => { const path = `$.validation.validators[${index}]`; const validator = objectAt(item, path); requireString(validator, "interface", path); requireStrings(validator, "checks", path); }); requireArray(validation, "benchmarks", "$.validation").forEach((item, index) => { const path = `$.validation.benchmarks[${index}]`; const benchmark = objectAt(item, path); requireString(benchmark, "name", path); requireString(benchmark, "framework", path); const configuration = requireObject(benchmark, "configuration", path); for (const [key, entry] of Object.entries(configuration)) scalarAt(entry, `${path}.configuration.${key}`); });
  const provenance = requireObject(root, "provenance"); requireString(provenance, "source_kind", "$.provenance"); if (provenance.source_reference !== null) stringAt(provenance.source_reference, "$.provenance.source_reference"); requireStrings(provenance, "attribution", "$.provenance");
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
