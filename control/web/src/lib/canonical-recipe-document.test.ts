import {
  createCanonicalRecipeDocument,
  parseCanonicalRecipeDocument,
} from "./canonical-recipe-document";

type Document = ReturnType<typeof createCanonicalRecipeDocument>;

test("accepts a complete canonical preset document", () => {
  const document = createCanonicalRecipeDocument();
  expect(parseCanonicalRecipeDocument(JSON.stringify(document))).toEqual({ok: true, document});
});

test.each([
  {
    name: "missing required scalar",
    mutate: (document: Document) => { delete (document.identity as Partial<Document["identity"]>).publisher; },
    error: "$.identity.publisher must be a string.",
  },
  {
    name: "wrong scalar type",
    mutate: (document: Document) => { (document.build.resources as Record<string, unknown>).memory_bytes = "16 GiB"; },
    error: "$.build.resources.memory_bytes must be an integer.",
  },
  {
    name: "wrong nested object type",
    mutate: (document: Document) => { (document.artifacts[0] as Record<string, unknown>).mount = null; },
    error: "$.artifacts[0].mount must be an object.",
  },
  {
    name: "wrong object inside collection",
    mutate: (document: Document) => { (document.topology as Record<string, unknown>).roles = [null]; },
    error: "$.topology.roles[0] must be an object.",
  },
  {
    name: "wrong nested collection",
    mutate: (document: Document) => { (document.runtime.lifecycle as Record<string, unknown>).pre_start = ["command"]; },
    error: "$.runtime.lifecycle.pre_start[0] must be an array.",
  },
  {
    name: "wrong scalar inside collection",
    mutate: (document: Document) => { (document.validation.validators[0] as Record<string, unknown>).checks = [7]; },
    error: "$.validation.validators[0].checks[0] must be a string.",
  },
  {
    name: "wrong optional scalar",
    mutate: (document: Document) => { (document.interfaces[0] as Record<string, unknown>).port = "8000"; },
    error: "$.interfaces[0].port must be an integer.",
  },
])("rejects $name", ({mutate, error}) => {
  const document = createCanonicalRecipeDocument();
  mutate(document);
  expect(parseCanonicalRecipeDocument(JSON.stringify(document))).toEqual({ok: false, error});
});
