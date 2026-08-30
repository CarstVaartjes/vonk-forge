import type {LibraryRecipeDetail, PublicRecipe} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {humanizeIdentifier} from "./library-technical-details";
import {StatusPill} from "./status-pill";

const COMFORTABLE_HEADROOM_BYTES = 8 * 1024 ** 3;

export type RecipeMemoryFit = "comfortable" | "tight" | "impossible" | "unknown";

type FitEvidence = {
  bestHeadroomBytes?: number;
  fit: RecipeMemoryFit;
  groupsEvaluated: number;
  requiredBytes?: number;
};

export function recipeMemoryFit(detail: LibraryRecipeDetail): FitEvidence {
  const placements = detail.placement;
  const groups = placements.flatMap(placement => [...placement.recommendations, ...placement.rejected_groups]);
  const measured = groups.flatMap(group => {
    if (group.nodes.length === 0) return [];
    return [{
      headroomBytes: Math.min(...group.nodes.map(node => node.memory_free_after_bytes)),
      requiredBytes: Math.max(...group.nodes.map(node => node.memory_required_bytes)),
    }];
  });
  if (measured.length === 0) return {fit: "unknown", groupsEvaluated: groups.length};

  const best = measured.reduce((current, candidate) => candidate.headroomBytes > current.headroomBytes ? candidate : current);
  if (best.headroomBytes >= COMFORTABLE_HEADROOM_BYTES) {
    return {bestHeadroomBytes: best.headroomBytes, fit: "comfortable", groupsEvaluated: measured.length, requiredBytes: best.requiredBytes};
  }
  if (best.headroomBytes >= 0) {
    return {bestHeadroomBytes: best.headroomBytes, fit: "tight", groupsEvaluated: measured.length, requiredBytes: best.requiredBytes};
  }
  if (placements.length > 0 && placements.every(placement => placement.search_complete)) {
    return {bestHeadroomBytes: best.headroomBytes, fit: "impossible", groupsEvaluated: measured.length, requiredBytes: best.requiredBytes};
  }
  return {bestHeadroomBytes: best.headroomBytes, fit: "unknown", groupsEvaluated: measured.length, requiredBytes: best.requiredBytes};
}

function fitCopy(evidence: FitEvidence): string {
  if (evidence.fit === "comfortable") return `The best evaluated complete group retains ${formatBytes(evidence.bestHeadroomBytes!)} per Spark after reservations, above the ${formatBytes(COMFORTABLE_HEADROOM_BYTES)} comfort band.`;
  if (evidence.fit === "tight") return `The best evaluated complete group fits, with ${formatBytes(evidence.bestHeadroomBytes!)} per Spark left after reservations, below the ${formatBytes(COMFORTABLE_HEADROOM_BYTES)} comfort band.`;
  if (evidence.fit === "impossible") return "Every complete group in the exhaustive placement search exceeds available memory.";
  if (evidence.bestHeadroomBytes !== undefined) return "The bounded search found no fit, but did not examine every possible complete group.";
  return "Placement has not produced bounded per-Spark memory evidence for this revision yet.";
}

function fitTone(fit: RecipeMemoryFit): "healthy" | "warning" | "danger" | "info" {
  if (fit === "comfortable") return "healthy";
  if (fit === "impossible") return "danger";
  if (fit === "tight") return "warning";
  return "info";
}

function fitLabel(fit: RecipeMemoryFit): string {
  return fit === "unknown" ? "Not measured" : fit[0]!.toUpperCase() + fit.slice(1);
}

export function LibraryRecipeFit({catalogRecipe, detail}: {catalogRecipe?: PublicRecipe; detail: LibraryRecipeDetail}) {
  const document = detail.visual_recipe;
  const evidence = recipeMemoryFit(detail);
  const topology = detail.topology;
  const family = catalogRecipe?.model_title ?? (document ? humanizeIdentifier(document.model.publisher) : "Unlinked model");
  const version = catalogRecipe?.model_version_title;
  const variantParts = [
    ...(catalogRecipe?.quantizations ?? []),
    ...(!catalogRecipe?.quantizations.length && catalogRecipe?.precision ? [catalogRecipe.precision] : []),
    topology ? `${topology.node_count} Spark${topology.node_count === 1 ? "" : "s"}` : undefined,
    topology ? humanizeIdentifier(topology.name) : undefined,
  ].filter((value): value is string => Boolean(value));

  return <section className="recipe-fit-strip" aria-label="Model variant and memory fit">
    <div className="recipe-fit-identity">
      <span>Model family</span>
      <strong>{family}</strong>
      {version && <small>{version}</small>}
    </div>
    <div className="recipe-fit-variant">
      <span>Recipe variant</span>
      <div className="recipe-variant-badges" aria-label={variantParts.length > 0 ? `Variant: ${variantParts.join(", ")}` : "Variant details not declared"}>
        {variantParts.length > 0 ? variantParts.map(part => <span key={part}>{part}</span>) : <span>Not declared</span>}
      </div>
      {catalogRecipe && <small>{formatBytes(catalogRecipe.maximum_runtime_memory_bytes_per_node)} declared maximum / Spark</small>}
    </div>
    <div className="recipe-fit-result">
      <span>Memory fit</span>
      <StatusPill tone={fitTone(evidence.fit)}>{fitLabel(evidence.fit)}</StatusPill>
      <small>{fitCopy(evidence)} Install and load reviews remain authoritative.</small>
    </div>
  </section>;
}
