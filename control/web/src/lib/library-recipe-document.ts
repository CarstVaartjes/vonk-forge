import type {LibraryRecipeDetail} from "../api/types";

type VisualRecipeDocument = NonNullable<LibraryRecipeDetail["visual_recipe"]>;

export type VisualRecipeParseResult =
  | {ok: true; document: VisualRecipeDocument}
  | {ok: false; error: string};

class DocumentValidationError extends Error {}

type JsonObject = Record<string, unknown>;
const MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807;
const MAX_SIGNED_BIGINT_LABEL = "9223372036854775807";
const MAX_SAFE_BIGINT = BigInt(Number.MAX_SAFE_INTEGER);
const MAX_SIGNED_BIGINT_EXACT = BigInt(MAX_SIGNED_BIGINT_LABEL);

class JsonLexemeScanError extends Error {}

type JsonPathPart = string | number;

function jsonPath(parts: readonly JsonPathPart[]): string {
  return parts.reduce<string>((path, part) => typeof part === "number"
    ? `${path}[${part}]`
    : /^[A-Za-z_][A-Za-z0-9_]*$/.test(part) ? `${path}.${part}` : `${path}[${JSON.stringify(part)}]`, "$" );
}

function integerLexemeMaximum(parts: readonly JsonPathPart[]): bigint | undefined {
  const path = jsonPath(parts);
  if (path === "$.schema_version") return 1n;
  if (path === "$.build.timeout_seconds") return 86_400n;
  if (path === "$.runtime.endpoint_port") return 65_535n;
  if (path === "$.validation.benchmark_count") return 32n;
  if (path === "$.build.context.expected_bytes"
    || ["download_bytes", "memory_bytes", "temporary_bytes"].some(field => path === `$.build.${field}`)
    || /^\$\.artifacts\[\d+\]\.(download_bytes|installed_bytes)$/.test(path)
    || path === "$.runtime.adapter_version") return MAX_SIGNED_BIGINT_EXACT;
  return undefined;
}

function scanIntegerLexemes(text: string): void {
  let index = 0;

  function whitespace() {
    while (/\s/.test(text[index] ?? "")) index += 1;
  }

  function stringToken(): string {
    const start = index;
    if (text[index] !== '"') throw new JsonLexemeScanError();
    index += 1;
    while (index < text.length) {
      if (text[index] === '"') {
        index += 1;
        try {
          return JSON.parse(text.slice(start, index)) as string;
        } catch (value) {
          if (value instanceof SyntaxError) throw new JsonLexemeScanError();
          throw value;
        }
      }
      if (text[index] === "\\") {
        index += text[index + 1] === "u" ? 6 : 2;
      } else {
        index += 1;
      }
    }
    throw new JsonLexemeScanError();
  }

  function numberToken(parts: readonly JsonPathPart[]) {
    const source = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(text.slice(index))?.[0];
    if (!source) throw new JsonLexemeScanError();
    index += source.length;
    const maximum = integerLexemeMaximum(parts);
    if (maximum === undefined) return;
    const path = jsonPath(parts);
    if (!/^-?(?:0|[1-9]\d*)$/.test(source)) {
      throw new DocumentValidationError(`${path} must use an integer JSON literal without a decimal point or exponent.`);
    }
    const exact = BigInt(source);
    if (exact > maximum) {
      if (path === "$.schema_version") throw new DocumentValidationError("$.schema_version must equal 1.");
      const minimum = ["$.build.timeout_seconds", "$.runtime.adapter_version", "$.runtime.endpoint_port"].includes(path) ? 1 : 0;
      throw new DocumentValidationError(`${path} must be an integer from ${minimum} through ${maximum}.`);
    }
    if (exact > MAX_SAFE_BIGINT) {
      throw new DocumentValidationError(`${path} cannot be preserved exactly; use an integer from 0 through ${Number.MAX_SAFE_INTEGER}.`);
    }
  }

  function value(parts: readonly JsonPathPart[]): void {
    whitespace();
    const token = text[index];
    if (token === "{") {
      index += 1;
      whitespace();
      if (text[index] === "}") { index += 1; return; }
      while (index < text.length) {
        whitespace();
        const key = stringToken();
        whitespace();
        if (text[index] !== ":") throw new JsonLexemeScanError();
        index += 1;
        value([...parts, key]);
        whitespace();
        if (text[index] === "}") { index += 1; return; }
        if (text[index] !== ",") throw new JsonLexemeScanError();
        index += 1;
      }
      throw new JsonLexemeScanError();
    }
    if (token === "[") {
      index += 1;
      whitespace();
      if (text[index] === "]") { index += 1; return; }
      let item = 0;
      while (index < text.length) {
        value([...parts, item]);
        item += 1;
        whitespace();
        if (text[index] === "]") { index += 1; return; }
        if (text[index] !== ",") throw new JsonLexemeScanError();
        index += 1;
      }
      throw new JsonLexemeScanError();
    }
    if (token === '"') { stringToken(); return; }
    if (token === "-" || /\d/.test(token ?? "")) { numberToken(parts); return; }
    for (const literal of ["true", "false", "null"]) {
      if (text.startsWith(literal, index)) { index += literal.length; return; }
    }
    throw new JsonLexemeScanError();
  }

  try {
    value([]);
    whitespace();
    if (index !== text.length) throw new JsonLexemeScanError();
  } catch (value) {
    if (value instanceof JsonLexemeScanError) return;
    throw value;
  }
}

function object(value: unknown, path: string, allowed?: readonly string[]): JsonObject {
  if (typeof value !== "object" || value === null || Array.isArray(value)) throw new DocumentValidationError(`${path} must be an object.`);
  const result = value as JsonObject;
  if (allowed) {
    const unexpected = Object.keys(result).find(key => !allowed.includes(key));
    if (unexpected) throw new DocumentValidationError(`${path}.${unexpected} is not allowed.`);
  }
  return result;
}

function string(value: unknown, path: string): string {
  if (typeof value !== "string") throw new DocumentValidationError(`${path} must be a string.`);
  return value;
}

function boundedString(value: unknown, path: string, maximum: number, minimum = 1): string {
  const result = string(value, path);
  const length = [...result].length;
  if (length < minimum || length > maximum) {
    const range = minimum === 0 ? `at most ${maximum}` : `${minimum} to ${maximum}`;
    throw new DocumentValidationError(`${path} must contain ${range} characters.`);
  }
  return result;
}

function integer(value: unknown, path: string, minimum: number, maximum: number, maximumLabel = String(maximum)): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < minimum || value > maximum) {
    throw new DocumentValidationError(`${path} must be an integer from ${minimum} through ${maximumLabel}.`);
  }
  if (!Number.isSafeInteger(value)) {
    const exactMinimum = Math.max(minimum, Number.MIN_SAFE_INTEGER);
    const exactMaximum = Math.min(maximum, Number.MAX_SAFE_INTEGER);
    throw new DocumentValidationError(`${path} cannot be preserved exactly; use an integer from ${exactMinimum} through ${exactMaximum}.`);
  }
  return value;
}

function array(value: unknown, path: string, maximum: number, minimum = 0): unknown[] {
  if (!Array.isArray(value)) throw new DocumentValidationError(`${path} must be an array.`);
  if (value.length < minimum) throw new DocumentValidationError(`${path} must contain at least ${minimum} item${minimum === 1 ? "" : "s"}.`);
  if (value.length > maximum) throw new DocumentValidationError(`${path} must contain at most ${maximum} items.`);
  return value;
}

function strings(value: unknown, path: string, maximum: number, itemMaximum: number, minimum = 0): string[] {
  const result = array(value, path, maximum, minimum);
  result.forEach((item, index) => boundedString(item, `${path}[${index}]`, itemMaximum));
  return value as string[];
}

function digest(value: unknown, path: string): string {
  const result = string(value, path);
  if (!/^[0-9a-f]{64}$/.test(result)) throw new DocumentValidationError(`${path} must be 64 lowercase hexadecimal characters.`);
  return result;
}

function validateDocument(value: unknown): VisualRecipeDocument {
  const root = object(value, "$", ["schema_version", "identity", "metadata", "workload", "build", "artifacts", "runtime", "validation", "provenance"]);
  if (root.schema_version !== 1) throw new DocumentValidationError("$.schema_version must equal 1.");

  const identity = object(root.identity, "$.identity", ["publisher", "slug"]);
  boundedString(identity.publisher, "$.identity.publisher", 128);
  boundedString(identity.slug, "$.identity.slug", 128);

  const metadata = object(root.metadata, "$.metadata", ["title", "description", "tags"]);
  boundedString(metadata.title, "$.metadata.title", 200);
  boundedString(metadata.description, "$.metadata.description", 512);
  strings(metadata.tags, "$.metadata.tags", 64, 64);

  const workload = object(root.workload, "$.workload", ["family", "capabilities"]);
  boundedString(workload.family, "$.workload.family", 128);
  strings(workload.capabilities, "$.workload.capabilities", 64, 64);

  const build = object(root.build, "$.build", ["context", "dockerfile", "platform", "network_mode", "network_hosts", "download_bytes", "temporary_bytes", "memory_bytes", "timeout_seconds"]);
  const context = object(build.context, "$.build.context", ["sha256", "expected_bytes", "media_type"]);
  digest(context.sha256, "$.build.context.sha256");
  integer(context.expected_bytes, "$.build.context.expected_bytes", 0, MAX_SIGNED_BIGINT, MAX_SIGNED_BIGINT_LABEL);
  boundedString(context.media_type, "$.build.context.media_type", 128);
  boundedString(build.dockerfile, "$.build.dockerfile", 256);
  integer(build.download_bytes, "$.build.download_bytes", 0, MAX_SIGNED_BIGINT, MAX_SIGNED_BIGINT_LABEL);
  integer(build.memory_bytes, "$.build.memory_bytes", 0, MAX_SIGNED_BIGINT, MAX_SIGNED_BIGINT_LABEL);
  strings(build.network_hosts, "$.build.network_hosts", 64, 256);
  boundedString(build.network_mode, "$.build.network_mode", 32);
  boundedString(build.platform, "$.build.platform", 64);
  integer(build.temporary_bytes, "$.build.temporary_bytes", 0, MAX_SIGNED_BIGINT, MAX_SIGNED_BIGINT_LABEL);
  integer(build.timeout_seconds, "$.build.timeout_seconds", 1, 86_400);

  array(root.artifacts, "$.artifacts", 128).forEach((item, index) => {
    const path = `$.artifacts[${index}]`;
    const artifact = object(item, path, ["id", "kind", "repository", "revision", "download_bytes", "installed_bytes", "roles"]);
    boundedString(artifact.id, `${path}.id`, 64);
    boundedString(artifact.kind, `${path}.kind`, 64);
    boundedString(artifact.repository, `${path}.repository`, 256);
    boundedString(artifact.revision, `${path}.revision`, 128);
    integer(artifact.download_bytes, `${path}.download_bytes`, 0, MAX_SIGNED_BIGINT, MAX_SIGNED_BIGINT_LABEL);
    integer(artifact.installed_bytes, `${path}.installed_bytes`, 0, MAX_SIGNED_BIGINT, MAX_SIGNED_BIGINT_LABEL);
    strings(artifact.roles, `${path}.roles`, 64, 64);
  });

  const runtime = object(root.runtime, "$.runtime", ["interface", "adapter", "adapter_version", "endpoint_protocol", "endpoint_port", "model_aliases", "health_path"]);
  boundedString(runtime.interface, "$.runtime.interface", 64);
  boundedString(runtime.adapter, "$.runtime.adapter", 64);
  integer(runtime.adapter_version, "$.runtime.adapter_version", 1, MAX_SIGNED_BIGINT, MAX_SIGNED_BIGINT_LABEL);
  boundedString(runtime.endpoint_protocol, "$.runtime.endpoint_protocol", 64);
  integer(runtime.endpoint_port, "$.runtime.endpoint_port", 1, 65_535);
  strings(runtime.model_aliases, "$.runtime.model_aliases", 64, 128);
  boundedString(runtime.health_path, "$.runtime.health_path", 512);

  const validation = object(root.validation, "$.validation", ["checks", "benchmark_count"]);
  strings(validation.checks, "$.validation.checks", 64, 80, 1);
  integer(validation.benchmark_count, "$.validation.benchmark_count", 0, 32);

  const provenance = object(root.provenance, "$.provenance", ["source_kind", "source_reference", "attribution"]);
  if (!["local", "workload_run", "global", "fork"].includes(provenance.source_kind as string)) {
    throw new DocumentValidationError("$.provenance.source_kind must be local, workload_run, global, or fork.");
  }
  if (provenance.source_reference !== null && typeof provenance.source_reference !== "string") {
    throw new DocumentValidationError("$.provenance.source_reference must be a string or null.");
  }
  if (typeof provenance.source_reference === "string") boundedString(provenance.source_reference, "$.provenance.source_reference", 2_048, 0);
  strings(provenance.attribution, "$.provenance.attribution", 32, 512);

  return root as VisualRecipeDocument;
}

function syntaxLocation(text: string, error: SyntaxError): string {
  const match = /position (\d+)/.exec(error.message);
  const unexpected = /Unexpected token '([^']+)'/.exec(error.message)?.[1];
  const position = match ? Number(match[1]) : unexpected ? text.lastIndexOf(unexpected) : text.length;
  const before = text.slice(0, position);
  const lines = before.split("\n");
  return `Invalid JSON at line ${lines.length}, column ${lines.at(-1)!.length + 1}.`;
}

export function parseVisualRecipeDocument(text: string): VisualRecipeParseResult {
  try {
    scanIntegerLexemes(text);
    return {ok: true, document: validateDocument(JSON.parse(text) as unknown)};
  } catch (value) {
    if (value instanceof DocumentValidationError) return {ok: false, error: value.message};
    if (value instanceof SyntaxError) return {ok: false, error: syntaxLocation(text, value)};
    return {ok: false, error: "Unable to validate the recipe document."};
  }
}
