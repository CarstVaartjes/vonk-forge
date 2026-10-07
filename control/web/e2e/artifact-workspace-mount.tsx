import {createElement} from "react";
import {createRoot} from "react-dom/client";
import {ApiClient} from "../src/api/client";
import {validateComponent} from "../src/api/contract-json";
import type {components} from "../src/api/generated";
import type {LibraryViewRecipeDetail, RecipeDefinition} from "../src/api/types";
import {ArtifactJobWorkspace} from "../src/components/artifact-job-workspace";

/** Mount the real component using schema-validated native producer documents. */
export function mountArtifactWorkspace(definitionJson: string, runJson: string, revisionId: string, digest: string): void {
  const definition = validateComponent("RecipeDefinition", definitionJson) as RecipeDefinition;
  const run = validateComponent("LibraryRunSummary", runJson) as components["schemas"]["LibraryRunSummary"];
  const detail: LibraryViewRecipeDetail = {
    generated_at: "2026-10-07T00:00:00Z", definition, topology: definition.topology,
    model_documents: [], operational_state: {builds: [], installations: [], mappings: [], runs: [run]},
    placement: [], reasons: [], recipe: {
      recipe_id: revisionId, recipe_revision_id: revisionId, content_sha256: digest,
      publisher: definition.identity.publisher, slug: definition.identity.slug,
      title: definition.metadata.title, description: definition.metadata.description,
      recipe_document: definition, capabilities: [], topology_name: definition.topology.name,
    },
  };
  const container = document.createElement("div"); document.body.append(container);
  createRoot(container).render(createElement(ArtifactJobWorkspace, {api: new ApiClient(), detail}));
}
