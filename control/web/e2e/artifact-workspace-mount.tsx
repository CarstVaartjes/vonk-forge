import {createElement} from "react";
import {createRoot} from "react-dom/client";
import {ApiClient} from "../src/api/client";
import {validateComponent} from "../src/api/contract-json";
import type {LibraryViewRecipeDetail, RecipeDefinition} from "../src/api/types";
import {withObservedRuns} from "../src/pages/library";
import {ArtifactJobWorkspace} from "../src/components/artifact-job-workspace";

/** Mount the real component using schema-validated native producer documents. */
export async function mountArtifactWorkspace(definitionJson: string, revisionId: string, digest: string): Promise<void> {
  const definition = validateComponent("RecipeDefinition", definitionJson) as RecipeDefinition;
  const api = new ApiClient();
  const fleet = await api.visualFleet();
  const detail: LibraryViewRecipeDetail = {
    generated_at: "2026-10-07T00:00:00Z", definition, topology: definition.topology,
    model_documents: [], operational_state: {builds: [], installations: [], mappings: [], runs: []},
    placement: [], reasons: [], recipe: {
      recipe_id: revisionId, recipe_revision_id: revisionId, content_sha256: digest,
      publisher: definition.identity.publisher, slug: definition.identity.slug,
      title: definition.metadata.title, description: definition.metadata.description,
      recipe_document: definition, capabilities: [], topology_name: definition.topology.name,
    },
  };
  const container = document.createElement("div"); document.body.append(container);
  const observedDetail = withObservedRuns(detail, fleet);
  if (!observedDetail || !observedDetail.operational_state.runs.length) throw new Error("Native fleet reports no running recipe for this exact revision");
  createRoot(container).render(createElement(ArtifactJobWorkspace, {api, detail: observedDetail}));
}
