import type {LibraryRecipeDetail} from "../api/types";

type VisualRecipeDocument = NonNullable<LibraryRecipeDetail["visual_recipe"]>;
type JsonObject = Record<string, unknown>;

export type VisualRecipeParseResult =
  | {ok: true; document: VisualRecipeDocument}
  | {ok: false; error: string};

class DocumentValidationError extends Error {}

function object(value: unknown, path: string, keys: readonly string[]): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new DocumentValidationError(`${path} must be an object.`);
  const result = value as JsonObject;
  const unexpected = Object.keys(result).find(key => !keys.includes(key));
  if (unexpected) throw new DocumentValidationError(`${path}.${unexpected} is not allowed.`);
  return result;
}

function string(value: unknown, path: string): string {
  if (typeof value !== "string" || value.length === 0) throw new DocumentValidationError(`${path} must be a non-empty string.`);
  return value;
}

function integer(value: unknown, path: string, minimum = 0): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) throw new DocumentValidationError(`${path} must be a safe integer of at least ${minimum}.`);
  return value;
}

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new DocumentValidationError(`${path} must be an array.`);
  return value;
}

function strings(value: unknown, path: string): string[] {
  return array(value, path).map((item, index) => string(item, `${path}[${index}]`));
}

function digest(value: unknown, path: string): string {
  const result = string(value, path);
  if (!/^[0-9a-f]{64}$/.test(result)) throw new DocumentValidationError(`${path} must be 64 lowercase hexadecimal characters.`);
  return result;
}

function identity(value: unknown, path: string, kinds: readonly string[]): void {
  const result = object(value, path, ["kind", "publisher", "slug", "content_sha256"]);
  if (!kinds.includes(string(result.kind, `${path}.kind`))) throw new DocumentValidationError(`${path}.kind is not a supported catalog identity.`);
  string(result.publisher, `${path}.publisher`); string(result.slug, `${path}.slug`); digest(result.content_sha256, `${path}.content_sha256`);
}

function validateDocument(value: unknown): VisualRecipeDocument {
  const root = object(value, "$", ["schema_version", "identity", "metadata", "model", "execution", "build", "artifacts", "runtime", "interfaces", "validation", "provenance"]);
  if (root.schema_version !== 1) throw new DocumentValidationError("$.schema_version must equal 1.");
  const recipeIdentity = object(root.identity, "$.identity", ["publisher", "slug"]);
  string(recipeIdentity.publisher, "$.identity.publisher"); string(recipeIdentity.slug, "$.identity.slug");
  const metadata = object(root.metadata, "$.metadata", ["title", "description", "tags"]);
  string(metadata.title, "$.metadata.title"); string(metadata.description, "$.metadata.description"); strings(metadata.tags, "$.metadata.tags");
  identity(root.model, "$.model", ["model-version"]);
  const execution = object(root.execution, "$.execution", ["harness", "patch_bundle"]);
  identity(execution.harness, "$.execution.harness", ["execution-harness"]);
  if (execution.patch_bundle !== null) identity(execution.patch_bundle, "$.execution.patch_bundle", ["patch-bundle"]);
  const build = object(root.build, "$.build", ["context", "dockerfile", "platform", "network_mode", "network_hosts", "download_bytes", "temporary_bytes", "memory_bytes", "timeout_seconds"]);
  const context = object(build.context, "$.build.context", ["sha256", "expected_bytes", "media_type"]);
  digest(context.sha256, "$.build.context.sha256"); integer(context.expected_bytes, "$.build.context.expected_bytes"); string(context.media_type, "$.build.context.media_type");
  string(build.dockerfile, "$.build.dockerfile"); string(build.platform, "$.build.platform"); string(build.network_mode, "$.build.network_mode"); strings(build.network_hosts, "$.build.network_hosts");
  for (const key of ["download_bytes", "temporary_bytes", "memory_bytes", "timeout_seconds"] as const) integer(build[key], `$.build.${key}`, key === "timeout_seconds" ? 1 : 0);
  array(root.artifacts, "$.artifacts").forEach((item, index) => {
    const artifact = object(item, `$.artifacts[${index}]`, ["id", "kind", "repository", "revision", "download_bytes", "installed_bytes", "roles"]);
    for (const key of ["id", "kind", "repository", "revision"] as const) string(artifact[key], `$.artifacts[${index}].${key}`);
    integer(artifact.download_bytes, `$.artifacts[${index}].download_bytes`); integer(artifact.installed_bytes, `$.artifacts[${index}].installed_bytes`); strings(artifact.roles, `$.artifacts[${index}].roles`);
  });
  const runtime = object(root.runtime, "$.runtime", ["distribution", "entrypoint", "lifecycle_pre_start_count", "lifecycle_post_stop_count", "stop_timeout_seconds"]);
  identity(runtime.distribution, "$.runtime.distribution", ["runtime-distribution"]); strings(runtime.entrypoint, "$.runtime.entrypoint"); integer(runtime.lifecycle_pre_start_count, "$.runtime.lifecycle_pre_start_count"); integer(runtime.lifecycle_post_stop_count, "$.runtime.lifecycle_post_stop_count"); integer(runtime.stop_timeout_seconds, "$.runtime.stop_timeout_seconds", 1);
  array(root.interfaces, "$.interfaces").forEach((item, index) => {
    const current = object(item, `$.interfaces[${index}]`, ["adapter", "port", "model_aliases", "health_path", "path"]);
    string(current.adapter, `$.interfaces[${index}].adapter`);
    if (current.port !== null && current.port !== undefined) integer(current.port, `$.interfaces[${index}].port`, 1);
    if (current.model_aliases !== undefined) strings(current.model_aliases, `$.interfaces[${index}].model_aliases`);
    if (current.health_path !== null && current.health_path !== undefined) string(current.health_path, `$.interfaces[${index}].health_path`);
    if (current.path !== null && current.path !== undefined) string(current.path, `$.interfaces[${index}].path`);
  });
  const validation = object(root.validation, "$.validation", ["checks", "benchmark_count"]); strings(validation.checks, "$.validation.checks"); integer(validation.benchmark_count, "$.validation.benchmark_count");
  const provenance = object(root.provenance, "$.provenance", ["source_kind", "source_reference", "attribution"]); string(provenance.source_kind, "$.provenance.source_kind"); if (provenance.source_reference !== null && typeof provenance.source_reference !== "string") throw new DocumentValidationError("$.provenance.source_reference must be a string or null."); strings(provenance.attribution, "$.provenance.attribution");
  return value as VisualRecipeDocument;
}

function syntaxLocation(text: string, error: SyntaxError): string {
  const position = Number(/position (\d+)/.exec(error.message)?.[1] ?? text.length);
  const before = text.slice(0, position); const lines = before.split("\n");
  return `Invalid JSON at line ${lines.length}, column ${lines.at(-1)!.length + 1}.`;
}

export function parseVisualRecipeDocument(text: string): VisualRecipeParseResult {
  try { return {ok: true, document: validateDocument(JSON.parse(text) as unknown)}; }
  catch (value) {
    if (value instanceof DocumentValidationError) return {ok: false, error: value.message};
    if (value instanceof SyntaxError) return {ok: false, error: syntaxLocation(text, value)};
    return {ok: false, error: "Unable to validate the recipe document."};
  }
}
