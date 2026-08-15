import {fullLibraryDetail} from "../test-fixtures/library";
import {parseVisualRecipeDocument} from "./library-recipe-document";

const visual = fullLibraryDetail.visual_recipe!;

test("reports JSON syntax with a useful line and column", () => {
  // Break caught: malformed JSON produces a generic editor failure with no
  // location an operator can use to recover.
  const result = parseVisualRecipeDocument('{\n  "workload": }');
  expect(result).toEqual({ok: false, error: "Invalid JSON at line 2, column 15."});
});

test.each([
  ["object root", "null", "$ must be an object."],
  ["schema version", JSON.stringify({...visual, schema_version: 2}), "$.schema_version must equal 1."],
  ["nested required field", JSON.stringify({...visual, workload: {capabilities: []}}), "$.workload.family must be a string."],
  ["nested array element", JSON.stringify({...visual, artifacts: [{...visual.artifacts[0], roles: [4]}]}), "$.artifacts[0].roles[0] must be a string."],
  ["nullable source reference", JSON.stringify({...visual, provenance: {...visual.provenance, source_reference: 9}}), "$.provenance.source_reference must be a string or null."],
])("reports the exact path for an invalid %s", (_case, text, error) => {
  // Break caught: a schema error is accepted or points only at the document,
  // forcing the operator to hunt for the invalid field.
  expect(parseVisualRecipeDocument(text)).toEqual({ok: false, error});
});

test("returns a typed visual document after validating every preview section", () => {
  // Break caught: a valid local preview edit is rejected or silently loses
  // fields needed by the visual detail.
  const changed = {
    ...visual,
    metadata: {...visual.metadata, title: "Qwen Local Preview"},
    workload: {...visual.workload, family: "qwen/preview"},
    runtime: {...visual.runtime, adapter: "sglang", endpoint_port: 9000},
  };
  expect(parseVisualRecipeDocument(JSON.stringify(changed))).toEqual({ok: true, document: changed});
});

test.each([
  ["root extra field", {...visual, unexpected: true}, "$.unexpected is not allowed."],
  ["nested extra field", {...visual, runtime: {...visual.runtime, secret: true}}, "$.runtime.secret is not allowed."],
  ["empty bounded string", {...visual, identity: {...visual.identity, publisher: ""}}, "$.identity.publisher must contain 1 to 128 characters."],
  ["oversized bounded string", {...visual, metadata: {...visual.metadata, title: "x".repeat(201)}}, "$.metadata.title must contain 1 to 200 characters."],
  ["malformed digest", {...visual, build: {...visual.build, context: {...visual.build.context, sha256: "not-a-digest"}}}, "$.build.context.sha256 must be 64 lowercase hexadecimal characters."],
  ["fractional integer", {...visual, runtime: {...visual.runtime, adapter_version: 1.5}}, "$.runtime.adapter_version must be an integer from 1 through 9223372036854775807."],
  ["out-of-range port", {...visual, runtime: {...visual.runtime, endpoint_port: 65536}}, "$.runtime.endpoint_port must be an integer from 1 through 65535."],
  ["negative byte count", {...visual, build: {...visual.build, download_bytes: -1}}, "$.build.download_bytes must be an integer from 0 through 9223372036854775807."],
  ["oversized array", {...visual, metadata: {...visual.metadata, tags: Array.from({length: 65}, () => "tag")}}, "$.metadata.tags must contain at most 64 items."],
  ["undersized array", {...visual, validation: {...visual.validation, checks: []}}, "$.validation.checks must contain at least 1 item."],
])("matches the canonical strict schema for an invalid %s", (_case, document, error) => {
  // Break caught: structural TypeScript checks accept documents the strict
  // canonical schema rejects, so the local preview can claim invalid JSON is valid.
  expect(parseVisualRecipeDocument(JSON.stringify(document))).toEqual({ok: false, error});
});

test("rejects an integer the visual path cannot preserve without rounding", () => {
  // Break caught: JSON.parse rounds a canonical signed integer before schema
  // checks, allowing the preview to display a different numeric authority.
  const safeText = JSON.stringify({...visual, build: {...visual.build, download_bytes: Number.MAX_SAFE_INTEGER}});
  const unsafeText = safeText.replace(String(Number.MAX_SAFE_INTEGER), "9007199254740993");
  expect(parseVisualRecipeDocument(unsafeText)).toEqual({
    ok: false,
    error: "$.build.download_bytes cannot be preserved exactly; use an integer from 0 through 9007199254740991.",
  });
});
