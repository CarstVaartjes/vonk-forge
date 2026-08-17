import {fullLibraryDetail} from "../test-fixtures/library";
import {parseVisualRecipeDocument} from "./library-recipe-document";

const visual = fullLibraryDetail.visual_recipe!;

test("reports JSON syntax with a useful location", () => {
  expect(parseVisualRecipeDocument('{\n  "model": }')).toEqual({ok: false, error: "Invalid JSON at line 2, column 13."});
});

test.each([
  ["schema version", {...visual, schema_version: 2}, "$.schema_version must equal 1."],
  ["prototype adapter", {...visual, runtime: {...visual.runtime, adapter: "vllm"}}, "$.runtime.adapter is not allowed."],
  ["bad model identity", {...visual, model: {...visual.model, content_sha256: "not-a-digest"}}, "$.model.content_sha256 must be 64 lowercase hexadecimal characters."],
  ["bad interface", {...visual, interfaces: [{...visual.interfaces[0], port: 0}]}, "$.interfaces[0].port must be a safe integer of at least 1."],
])("rejects %s from the strict visual contract", (_case, document, error) => {
  expect(parseVisualRecipeDocument(JSON.stringify(document))).toEqual({ok: false, error});
});

test("returns a typed strict visual document", () => {
  const changed = {...visual, execution: {...visual.execution, patch_bundle: {kind: "patch-bundle" as const, publisher: "vonk-forge", slug: "safety-patch", content_sha256: "2".repeat(64)}}};
  expect(parseVisualRecipeDocument(JSON.stringify(changed))).toEqual({ok: true, document: changed});
});
