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

function number(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new DocumentValidationError(`${path} must be a finite number.`);
  return value;
}

function scalar(value: unknown, path: string): void {
  if (value !== null && typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") throw new DocumentValidationError(`${path} must be a string, number, boolean, or null.`);
  if (typeof value === "number" && !Number.isFinite(value)) throw new DocumentValidationError(`${path} must be a finite number.`);
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

function slots(value: unknown, path: string): void {
  array(value, path).forEach((item, index) => {
    const slotPath = `${path}[${index}]`;
    const slot = object(item, slotPath, ["id", "label", "description", "media_types", "extensions", "min_files", "max_files", "max_file_bytes", "max_total_bytes"]);
    for (const key of ["id", "label", "description"] as const) string(slot[key], `${slotPath}.${key}`);
    strings(slot.media_types, `${slotPath}.media_types`); strings(slot.extensions, `${slotPath}.extensions`);
    for (const key of ["min_files", "max_files", "max_file_bytes", "max_total_bytes"] as const) integer(slot[key], `${slotPath}.${key}`);
  });
}

function validateDocument(value: unknown): VisualRecipeDocument {
  const root = object(value, "$", ["schema_version", "identity", "metadata", "model", "model_license", "parameters", "execution", "build", "artifacts", "runtime", "interfaces", "validation", "provenance"]);
  if (root.schema_version !== 1) throw new DocumentValidationError("$.schema_version must equal 1.");
  const recipeIdentity = object(root.identity, "$.identity", ["publisher", "slug"]);
  string(recipeIdentity.publisher, "$.identity.publisher"); string(recipeIdentity.slug, "$.identity.slug");
  const metadata = object(root.metadata, "$.metadata", ["title", "description", "tags"]);
  string(metadata.title, "$.metadata.title"); string(metadata.description, "$.metadata.description"); strings(metadata.tags, "$.metadata.tags");
  identity(root.model, "$.model", ["model-version"]);
  if (root.model_license !== null) {
    const modelLicense = object(root.model_license, "$.model_license", ["territorial_restrictions"]);
    if (modelLicense.territorial_restrictions !== null && modelLicense.territorial_restrictions !== undefined) {
      const restrictions = object(modelLicense.territorial_restrictions, "$.model_license.territorial_restrictions", ["denied_jurisdictions", "notice"]);
      strings(restrictions.denied_jurisdictions, "$.model_license.territorial_restrictions.denied_jurisdictions");
      string(restrictions.notice, "$.model_license.territorial_restrictions.notice");
    }
  }
  array(root.parameters, "$.parameters").forEach((item, index) => {
    const parameterPath = `$.parameters[${index}]`;
    const parameter = object(item, parameterPath, ["name", "description", "type", "default", "minimum", "maximum", "allowed_values", "pattern", "change_effect"]);
    string(parameter.name, `${parameterPath}.name`); string(parameter.description, `${parameterPath}.description`);
    if (!["string", "integer", "boolean", "enum"].includes(string(parameter.type, `${parameterPath}.type`))) throw new DocumentValidationError(`${parameterPath}.type is not supported.`);
    scalar(parameter.default, `${parameterPath}.default`);
    if (parameter.minimum !== null && parameter.minimum !== undefined) number(parameter.minimum, `${parameterPath}.minimum`);
    if (parameter.maximum !== null && parameter.maximum !== undefined) number(parameter.maximum, `${parameterPath}.maximum`);
    if (parameter.allowed_values !== undefined) array(parameter.allowed_values, `${parameterPath}.allowed_values`).forEach((allowed, allowedIndex) => scalar(allowed, `${parameterPath}.allowed_values[${allowedIndex}]`));
    if (parameter.pattern !== null && parameter.pattern !== undefined) string(parameter.pattern, `${parameterPath}.pattern`);
    if (!["rebuild", "reinstall", "restart"].includes(string(parameter.change_effect, `${parameterPath}.change_effect`))) throw new DocumentValidationError(`${parameterPath}.change_effect is not supported.`);
  });
  const execution = object(root.execution, "$.execution", ["harness", "patch_bundle"]);
  identity(execution.harness, "$.execution.harness", ["execution-harness"]);
  if (execution.patch_bundle !== null) identity(execution.patch_bundle, "$.execution.patch_bundle", ["patch-bundle"]);
  const build = object(root.build, "$.build", ["context", "dockerfile", "platform", "network_mode", "network_hosts", "download_bytes", "temporary_bytes", "memory_bytes", "timeout_seconds"]);
  const context = object(build.context, "$.build.context", ["sha256", "expected_bytes", "media_type"]);
  digest(context.sha256, "$.build.context.sha256"); integer(context.expected_bytes, "$.build.context.expected_bytes"); string(context.media_type, "$.build.context.media_type");
  string(build.dockerfile, "$.build.dockerfile"); string(build.platform, "$.build.platform"); string(build.network_mode, "$.build.network_mode"); strings(build.network_hosts, "$.build.network_hosts");
  for (const key of ["download_bytes", "temporary_bytes", "memory_bytes", "timeout_seconds"] as const) integer(build[key], `$.build.${key}`, key === "timeout_seconds" ? 1 : 0);
  array(root.artifacts, "$.artifacts").forEach((item, index) => {
    const artifact = object(item, `$.artifacts[${index}]`, ["id", "kind", "repository", "revision", "include_paths", "download_bytes", "installed_bytes", "roles"]);
    for (const key of ["id", "kind", "repository", "revision"] as const) string(artifact[key], `$.artifacts[${index}].${key}`);
    const includePaths = strings(artifact.include_paths, `$.artifacts[${index}].include_paths`);
    if (includePaths.length > 256) throw new DocumentValidationError(`$.artifacts[${index}].include_paths must contain at most 256 items.`);
    if (includePaths.some(path => path.length > 512)) throw new DocumentValidationError(`$.artifacts[${index}].include_paths entries must contain at most 512 characters.`);
    if (includePaths.some((path, pathIndex) => pathIndex > 0 && includePaths[pathIndex - 1]! >= path)) throw new DocumentValidationError(`$.artifacts[${index}].include_paths must be sorted and unique.`);
    integer(artifact.download_bytes, `$.artifacts[${index}].download_bytes`); integer(artifact.installed_bytes, `$.artifacts[${index}].installed_bytes`); strings(artifact.roles, `$.artifacts[${index}].roles`);
  });
  const runtime = object(root.runtime, "$.runtime", ["distribution", "entrypoint", "lifecycle_pre_start_count", "lifecycle_post_stop_count", "stop_timeout_seconds"]);
  identity(runtime.distribution, "$.runtime.distribution", ["runtime-distribution"]); strings(runtime.entrypoint, "$.runtime.entrypoint"); integer(runtime.lifecycle_pre_start_count, "$.runtime.lifecycle_pre_start_count"); integer(runtime.lifecycle_post_stop_count, "$.runtime.lifecycle_post_stop_count"); integer(runtime.stop_timeout_seconds, "$.runtime.stop_timeout_seconds", 1);
  array(root.interfaces, "$.interfaces").forEach((item, index) => {
    const interfacePath = `$.interfaces[${index}]`;
    const current = object(item, interfacePath, ["adapter", "port", "model_aliases", "health_path", "path", "input", "output", "timeout_seconds"]);
    string(current.adapter, `${interfacePath}.adapter`);
    if (current.port !== null && current.port !== undefined) integer(current.port, `${interfacePath}.port`, 1);
    if (current.model_aliases !== undefined) strings(current.model_aliases, `${interfacePath}.model_aliases`);
    if (current.health_path !== null && current.health_path !== undefined) string(current.health_path, `${interfacePath}.health_path`);
    if (current.path !== null && current.path !== undefined) string(current.path, `${interfacePath}.path`);
    if (current.timeout_seconds !== null && current.timeout_seconds !== undefined) integer(current.timeout_seconds, `${interfacePath}.timeout_seconds`, 1);
    if (current.input !== null && current.input !== undefined) {
      const input = object(current.input, `${interfacePath}.input`, ["path", "required", "media_types", "max_bytes", "min_files", "max_files", "slots"]);
      string(input.path, `${interfacePath}.input.path`);
      if (typeof input.required !== "boolean") throw new DocumentValidationError(`${interfacePath}.input.required must be a boolean.`);
      strings(input.media_types, `${interfacePath}.input.media_types`); integer(input.max_bytes, `${interfacePath}.input.max_bytes`); integer(input.min_files, `${interfacePath}.input.min_files`); integer(input.max_files, `${interfacePath}.input.max_files`);
      if (input.slots !== undefined) slots(input.slots, `${interfacePath}.input.slots`);
    }
    if (current.output !== null && current.output !== undefined) {
      const output = object(current.output, `${interfacePath}.output`, ["path", "allowed_media_types", "max_total_bytes", "slots"]);
      string(output.path, `${interfacePath}.output.path`); strings(output.allowed_media_types, `${interfacePath}.output.allowed_media_types`);
      if (output.max_total_bytes !== null && output.max_total_bytes !== undefined) integer(output.max_total_bytes, `${interfacePath}.output.max_total_bytes`);
      if (output.slots !== undefined) slots(output.slots, `${interfacePath}.output.slots`);
    }
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
