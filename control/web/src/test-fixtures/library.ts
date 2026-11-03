import type {
  ModelDefinition,
  RecipeDefinition,
  LibraryViewModel,
  LibraryViewRecipe,
  LibraryViewRecipeDetail,
  LibraryViewSnapshot,
  ModelDisplay,
  ModelLibrary,
  RecipeDetail,
  RecipeDisplay,
  RecipeLibrary,
} from "../api/types";
import corpusSource from "./library-corpus.json?raw";

// Stable fixture identities must satisfy the same hex digest contract as HTTP.
const digest = (seed: string): string => {
  const hex = Array.from(seed, (character) =>
    character.charCodeAt(0).toString(16).padStart(2, "0"),
  ).join("");
  return hex.repeat(Math.ceil(64 / hex.length)).slice(0, 64);
};
const revisionId = (index: number): string =>
  `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`;
const recipeId = (index: number): string =>
  `00000001-0000-4000-8000-${String(index + 1).padStart(12, "0")}`;
const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const corpus = JSON.parse(corpusSource) as Record<string, unknown>;

// The Controller serves the library text as its own display, built from the
// signed index; the fixtures register one display per document slug.
type RecipeSeed = Omit<LibraryViewRecipe, "recipe_display">;
type ModelSeed = Omit<LibraryViewModel, "model_display">;
const recipeDisplays = new Map<string, RecipeDisplay>();
const modelDisplays = new Map<string, ModelDisplay>();
function withRecipeDisplay(entry: RecipeSeed): LibraryViewRecipe {
  return { ...entry, recipe_display: recipeDisplays.get(entry.slug) ?? null };
}
function modelDisplayOf(document: ModelDefinition): LibraryViewModel["model_display"] {
  return modelDisplays.get(document.identity.slug) ?? null;
}

// Projected from the Controller schema2 catalog corpus (library-corpus.json),
// stripped of display, which seedDisplays carries beside it (92 Models / 85 Recipes).
const actualModelDocument36 = corpus.actualModelDocument36 as ModelDefinition;
const actualModelDocument37 = corpus.actualModelDocument37 as ModelDefinition;
const actualModelDocumentSeed = corpus.actualModelDocumentSeed as ModelDefinition;
const actualRecipeDocumentSeed = corpus.actualRecipeDocumentSeed as RecipeDefinition;

// The display text of the corpus above, which the Controller serves from the
// signed index beside each document.
const seedDisplays = corpus.seedDisplays as {
  models: Record<string, ModelDisplay>;
  recipes: Record<string, RecipeDisplay>;
};

for (const [slug, display] of Object.entries(seedDisplays.recipes))
  recipeDisplays.set(slug, display);
for (const [slug, display] of Object.entries(seedDisplays.models)) modelDisplays.set(slug, display);

// The highlighted four-Recipe LTX Model and its ordered model dependencies remain
// byte-for-byte projections of the frozen Controller response corpus. Synthetic
// rows below provide the remaining cardinality without mutating these pairs.
const actualLtxModels = corpus.actualLtxModels as (Omit<LibraryViewModel, "model_document"> & {
  model_document: ModelDefinition;
})[];
const actualLtxRecipes = corpus.actualLtxRecipes as LibraryViewRecipe[];

function modelDocument(index: number): ModelDefinition {
  if (index >= 2 && index <= 6) return clone(actualLtxModels[index - 2]!.model_document);
  const document = clone(
    index === 0
      ? actualModelDocument36
      : index === 1
        ? actualModelDocument37
        : actualModelDocumentSeed,
  );
  if (index < 2) return document;
  const publisher = index % 2 === 0 ? "deepseek-ai" : "inclusionai";
  const slug = index % 2 === 0 ? `deepseek-v4-flash-${index + 1}` : `ling-3-flash-${index + 1}`;
  const title = index % 2 === 0 ? `DeepSeek V4 Flash ${index + 1}` : `Ling 3 Flash ${index + 1}`;
  document.identity = {
    ...document.identity,
    publisher,
    slug,
    version: `catalog-${index + 1}`,
    variant: `schema2-${index + 1}`,
    model: { ...document.identity.model, publisher, slug },
    family: { ...document.identity.family, publisher, slug: slug.split("-")[0]! },
  };
  modelDisplays.set(slug, {
    ...modelDisplays.get(actualModelDocumentSeed.identity.slug)!,
    identity: { publisher, slug },
    family_title: title,
    model_title: title,
    description: `${title} canonical ModelDefinition from the Controller catalog corpus.`,
  });
  return document;
}

function model(index: number, recipes: LibraryViewRecipe[]): ModelSeed {
  const document = modelDocument(index);
  const actual = index >= 2 && index <= 6 ? actualLtxModels[index - 2]!.model : undefined;
  const local = { controller: "not_cached" as const, running_on: [], preparation: null };
  const facets = {
    family: `${document.identity.family.publisher}/${document.identity.family.slug}`,
    version: document.identity.version,
    quantization: document.format.quantization,
  };
  const usage = [...document.capabilities];
  const alignment = [
    ...new Set(
      recipes.flatMap((recipe) =>
        recipe.recipe_display?.alignment ? [recipe.recipe_display.alignment] : [],
      ),
    ),
  ];
  if (actual)
    return {
      ...clone(actualLtxModels[index - 2]!),
      model_document: document,
      model_file_count: document.files.length,
      model_bytes: document.files.reduce((sum, file) => sum + Number(file.size_bytes), 0),
      local,
      ...facets,
      usage,
      alignment,
      recipes,
    };
  return {
    page_local: true,
    model: {
      kind: "model",
      publisher: document.identity.publisher,
      slug: document.identity.slug,
      content_sha256: digest(`m${index}`),
    },
    model_document: document,
    model_file_count: document.files.length,
    model_bytes: document.files.reduce((sum, file) => sum + Number(file.size_bytes), 0),
    model_capabilities: [...document.capabilities],
    local,
    ...facets,
    usage,
    alignment,
    recipes,
  };
}

const syntheticModelTargets = [0, 1, ...Array.from({ length: 72 }, (_, offset) => offset + 7)];
function syntheticModelTarget(recipeIndex: number): number {
  return syntheticModelTargets[
    (recipeIndex - actualLtxRecipes.length) % syntheticModelTargets.length
  ]!;
}

function recipe(index: number): RecipeSeed {
  if (index < actualLtxRecipes.length) return clone(actualLtxRecipes[index]!);
  const id = index === 0 ? actualRecipeDocumentSeed.identity.slug : `recipe-${index + 1}`;
  const seedRecipeDisplay = recipeDisplays.get(actualRecipeDocumentSeed.identity.slug)!;
  const title =
    index === 0
      ? seedRecipeDisplay.title
      : index === 1
        ? "Ling 3.0 Flash auxiliary"
        : `Catalog Recipe ${index + 1}`;
  const definition = clone(actualRecipeDocumentSeed);
  if (index === 0)
    return {
      capabilities: [],
      content_sha256: digest(`r${index}`),
      description: seedRecipeDisplay.description,
      installation_returned_count: 0,
      installation_total_count: 0,
      installations: [],
      installations_truncated: false,
      publisher: definition.identity.publisher,
      reasons: [],
      recipe_document: definition,
      recipe_id: recipeId(index),
      recipe_revision_id: revisionId(index),
      run_returned_count: 0,
      run_total_count: 0,
      runs: [],
      runs_truncated: false,
      slug: id,
      title,
      topology_name: definition.topology.name,
    };
  definition.identity = { ...definition.identity, slug: id };
  const description = `${title} canonical RecipeDefinition from the Controller catalog corpus.`;
  recipeDisplays.set(id, {
    ...seedRecipeDisplay,
    identity: { publisher: definition.identity.publisher, slug: id },
    title,
    description,
  });
  const modelIndexes = index === 4 ? [0, 1] : [syntheticModelTarget(index)];
  definition.models = modelIndexes.map((modelIndex, modelPosition) => {
    const source =
      actualRecipeDocumentSeed.models[modelPosition % actualRecipeDocumentSeed.models.length]!;
    const modelDoc = modelDocument(modelIndex);
    return {
      ...source,
      id: modelPosition === 0 ? "primary" : `auxiliary-${modelPosition}`,
      model: {
        ...source.model,
        publisher: modelDoc.identity.publisher,
        slug: modelDoc.identity.slug,
        content_sha256: digest(`m${modelIndex}`),
      },
    };
  });
  return {
    capabilities: ["chat"],
    content_sha256: digest(`r${index}`),
    description,
    installation_returned_count: 0,
    installation_total_count: 0,
    installations: [],
    installations_truncated: false,
    publisher: definition.identity.publisher,
    reasons: [],
    recipe_document: definition,
    recipe_id: recipeId(index),
    recipe_revision_id: revisionId(index),
    run_returned_count: 0,
    run_total_count: 0,
    runs: [],
    runs_truncated: false,
    slug: id,
    title,
    topology_name: definition.topology.name,
  };
}

export function libraryRecipeSummary(
  input: Partial<LibraryViewRecipe> & Pick<LibraryViewRecipe, "recipe_id" | "slug" | "title">,
): LibraryViewRecipe {
  const base = withRecipeDisplay(recipe(0));
  return {
    ...base,
    ...input,
    recipe_display: {
      ...base.recipe_display!,
      identity: { ...base.recipe_display!.identity, slug: input.slug },
      title: input.title ?? base.title,
      description: input.description ?? base.description,
    },
    recipe_document: {
      ...base.recipe_document,
      identity: { ...base.recipe_document.identity, slug: input.slug },
    },
  };
}

const recipes = Array.from({ length: 85 }, (_, index) => withRecipeDisplay(recipe(index)));
const models = Array.from({ length: 92 }, (_, index) => {
  const withDisplay = (seed: ModelSeed): LibraryViewModel => ({
    ...seed,
    model_display: modelDisplayOf(seed.model_document as ModelDefinition),
  });
  if (index >= 2 && index <= 5) return withDisplay(model(index, [recipes[index - 2]!]));
  if (index === 6) return withDisplay(model(index, recipes.slice(0, actualLtxRecipes.length)));
  if (index >= 79) return withDisplay(model(index, []));
  const assigned = recipes.slice(actualLtxRecipes.length).filter((_, recipeIndex) => {
    const catalogIndex = recipeIndex + actualLtxRecipes.length;
    return syntheticModelTarget(catalogIndex) === index;
  });
  if (index === 1) assigned.unshift(recipes[4]!);
  return withDisplay(model(index, assigned));
});

export const libraryViewSnapshot: LibraryViewSnapshot = {
  generated_at: "2026-09-06T12:00:00Z",
  freshness_policy: {
    inventory_fresh_seconds: 300,
    telemetry_delayed_seconds: 20,
    telemetry_live_seconds: 6,
  },
  models,
  unlinked_recipes: [],
};
const projectionResources = {
  disk_bytes: null,
  image_bytes: null,
  memory_bytes: null,
  runtime_memory_bytes: null,
};
const projectionLocal = { controller: "cached" as const, preparation: null, running_on: [] };
const facetValues = { family: [], quantization: [], usage: [], version: [] };
export const modelLibrary: ModelLibrary = {
  generated_at: libraryViewSnapshot.generated_at,
  freshness_policy: libraryViewSnapshot.freshness_policy,
  facets: facetValues,
  filters: {},
  next_cursor: null,
  models: models.map((entry) => {
    if (entry.model_bytes === null) throw new Error("Fixture model size is required");
    return {
      selector: entry.model.content_sha256,
      identity: entry.model,
      model_display: entry.model_display,
      projected: {
        identity: entry.model_document.identity,
        format: entry.model_document.format,
        capabilities: entry.model_document.capabilities,
        modalities: ["text"],
        artifact_count: entry.model_file_count,
        download_bytes: entry.model_bytes,
        installed_bytes: entry.model_bytes,
      },
      family: entry.model_document.identity.family.slug,
      version: entry.model_document.identity.version,
      variant: entry.model_document.identity.variant,
      quantization: entry.model_document.format.quantization ?? "none",
      usage: [...entry.model_document.capabilities],
      resources: projectionResources,
      local: projectionLocal,
      alignment: entry.alignment,
      updated_at: libraryViewSnapshot.generated_at,
    };
  }),
};
export const recipeLibrary: RecipeLibrary = {
  generated_at: libraryViewSnapshot.generated_at,
  freshness_policy: libraryViewSnapshot.freshness_policy,
  facets: facetValues,
  filters: {},
  next_cursor: null,
  recipes: [
    ...models.flatMap((entry) => entry.recipes),
    ...libraryViewSnapshot.unlinked_recipes,
  ].map((entry) => ({
    selector: `${entry.publisher}/${entry.slug}`,
    identity: {
      content_sha256: entry.content_sha256,
      description: entry.description,
      publisher: entry.publisher,
      recipe_id: entry.recipe_id,
      recipe_revision_id: entry.recipe_revision_id,
      slug: entry.slug,
      title: entry.title,
    },
    recipe_display: entry.recipe_display,
    projected: {
      identity: entry.recipe_document.identity,
      topology: entry.recipe_document.topology,
      release: entry.recipe_document.release,
      provenance: entry.recipe_document.provenance,
      models: entry.recipe_document.models.map((selection) => selection.model),
      runtime_engine: entry.recipe_document.runtime.engine,
      interfaces: actualRecipeDocumentSeed.interfaces,
      validation: actualRecipeDocumentSeed.validation,
      options: actualRecipeDocumentSeed.options ?? [],
      base_image_digest: actualRecipeDocumentSeed.execution.build.base_image.digest,
    },
    model_selectors: entry.recipe_document.models.map((model) => model.model.content_sha256),
    usage: entry.capabilities,
    resources: projectionResources,
    local: projectionLocal,
    alignment: entry.recipe_display?.alignment,
    node_count: entry.recipe_document.topology.node_count,
    engine: entry.recipe_document.runtime.engine,
    creator: entry.recipe_document.provenance.source_reference?.split("/")[3] ?? null,
    updated_at: libraryViewSnapshot.generated_at,
  })),
};
export const chatRecipe = recipes[0]!;
export const codeRecipe = recipes[1]!;
export const unlinkedRecipe = libraryRecipeSummary({
  recipe_id: recipeId(10_000),
  recipe_revision_id: revisionId(10_000),
  slug: "unlinked",
  title: "Unlinked recipe",
});
const detailModelDocuments = recipes[0]!.recipe_document.models.map((selection) => ({
  model_document: {
    ...actualModelDocumentSeed,
    ...models.find((model) => model.model.content_sha256 === selection.model.content_sha256)!
      .model_document,
  },
  model_display: models.find(
    (model) => model.model.content_sha256 === selection.model.content_sha256,
  )!.model_display,
  selection: { ...actualRecipeDocumentSeed.models[0]!, ...selection },
}));
export const minimalLibraryDetail: LibraryViewRecipeDetail = {
  generated_at: "2026-09-06T12:00:00Z",
  definition: {
    ...actualRecipeDocumentSeed,
    ...recipes[0]!.recipe_document,
    models: recipes[0]!.recipe_document.models.map((selection) => ({
      ...actualRecipeDocumentSeed.models[0]!,
      ...selection,
    })),
    runtime: { ...actualRecipeDocumentSeed.runtime, ...recipes[0]!.recipe_document.runtime },
  },
  recipe: { ...recipes[0]! },
  model_documents: detailModelDocuments,
  operational_state: { builds: [], installations: [], mappings: [], runs: [] },
  placement: [],
  reasons: [],
  topology: recipes[0]!.recipe_document.topology,
};
export const fullLibraryDetail = minimalLibraryDetail;
export const currentRecipeDetail: RecipeDetail = {
  selector: recipeLibrary.recipes[0]!.selector,
  identity: recipeLibrary.recipes[0]!.identity,
  document: minimalLibraryDetail.definition,
  projected: recipeLibrary.recipes[0]!.projected,
  recipe_display: recipeLibrary.recipes[0]!.recipe_display,
  model_selectors: recipeLibrary.recipes[0]!.model_selectors,
  model_documents: detailModelDocuments,
  usage: recipeLibrary.recipes[0]!.usage,
  resources: projectionResources,
  local: projectionLocal,
  alignment: recipeLibrary.recipes[0]!.alignment,
  node_count: recipeLibrary.recipes[0]!.node_count,
  engine: recipeLibrary.recipes[0]!.engine,
  creator: recipeLibrary.recipes[0]!.creator,
  updated_at: libraryViewSnapshot.generated_at,
};
