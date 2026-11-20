import type { Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import type { components } from "../src/api/generated";

type Schemas = components["schemas"];
// The frozen Controller catalog corpus the unit fixtures project from; plain JSON,
// so the browser journeys read the same documents without a bundler import.
const corpus = JSON.parse(
  readFileSync(new URL("../src/test-fixtures/library-corpus.json", import.meta.url), "utf8"),
);

const definition = corpus.actualRecipeDocumentSeed as Schemas["RecipeDefinition"];
const modelDocument = corpus.actualModelDocumentSeed as Schemas["ModelDefinition"];
const recipeDisplay = corpus.seedDisplays.recipes[definition.identity.slug];
const modelDisplay = corpus.seedDisplays.models[modelDocument.identity.slug];
const modelSelection = definition.models[0]!;
const generated_at = "2026-09-06T12:00:00Z";
const freshness_policy = {
  inventory_fresh_seconds: 300,
  telemetry_delayed_seconds: 20,
  telemetry_live_seconds: 6,
};
const facets = { family: [], line: [], quantization: [], usage: [], version: [] };
const resources = {
  disk_bytes: null,
  image_bytes: null,
  memory_bytes: null,
  runtime_memory_bytes: null,
};
const local = { controller: "cached", preparation: null, running_on: [] };
const recipeIdentity = {
  content_sha256: "a".repeat(64),
  description: recipeDisplay.description,
  publisher: definition.identity.publisher,
  recipe_id: "00000001-0000-4000-8000-000000000001",
  recipe_revision_id: "00000000-0000-4000-8000-000000000001",
  slug: definition.identity.slug,
  title: recipeDisplay.title,
};
export const fixtureRecipeSelector = `${definition.identity.publisher}/${definition.identity.slug}`;
export const fixtureRecipeTitle: string = recipeDisplay.title;

const recipeSummary = {
  selector: fixtureRecipeSelector,
  identity: recipeIdentity,
  recipe_display: recipeDisplay,
  projected: {
    identity: definition.identity,
    topology: definition.topology,
    release: definition.release,
    provenance: definition.provenance,
    models: definition.models.map((selection) => selection.model),
    runtime_engine: definition.runtime.engine,
    interfaces: definition.interfaces,
    validation: definition.validation,
    options: definition.options ?? [],
    base_image_digest: definition.execution.build.base_image.digest,
  },
  model_selectors: [modelSelection.model.content_sha256],
  usage: ["chat"],
  resources,
  local,
  alignment: recipeDisplay.alignment,
  node_count: definition.topology.node_count,
  engine: definition.runtime.engine,
  creator: null,
  updated_at: generated_at,
};

/** One Model with one Recipe (and that Recipe's detail), served through the real client. */
export async function serveLibrary(page: Page) {
  const model = {
    selector: modelSelection.model.content_sha256,
    identity: { ...modelSelection.model, kind: "model" },
    model_display: modelDisplay,
    projected: {
      identity: modelDocument.identity,
      format: modelDocument.format,
      artifact_count: modelDocument.files.length,
      download_bytes: 1_000_000,
      installed_bytes: 1_000_000,
    },
    family: modelDisplay?.lineage?.family ?? null,
    line: modelDisplay?.lineage?.line ?? null,
    version: modelDocument.identity.version,
    variant: modelDocument.identity.variant,
    quantization: modelDocument.format.quantization ?? "none",
    usage: [...(modelDisplay?.capabilities ?? [])],
    resources,
    local,
    alignment: [],
    updated_at: generated_at,
  };
  await page.route("**/api/model/library**", (route) =>
    route.fulfill({
      json: {
        generated_at,
        freshness_policy,
        facets,
        filters: {},
        next_cursor: null,
        models: [model],
      },
    }),
  );
  await page.route("**/api/recipe/library**", (route) =>
    route.fulfill({
      json: {
        generated_at,
        freshness_policy,
        facets,
        filters: {},
        next_cursor: null,
        recipes: [recipeSummary],
      },
    }),
  );
  await page.route(
    (url) => /^\/api\/recipe\/[^/]+$/.test(url.pathname) && url.pathname !== "/api/recipe/library",
    (route) =>
      route.fulfill({
        json: {
          ...recipeSummary,
          document: definition,
          model_documents: [
            {
              model_document: modelDocument,
              model_display: modelDisplay,
              selection: modelSelection,
            },
          ],
        },
      }),
  );
}
