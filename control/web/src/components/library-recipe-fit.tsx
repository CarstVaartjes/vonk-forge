import type {LibraryRecipeDetail} from "../api/types";
import {formatBytes} from "../lib/fleet";

export type RecipeMemoryFit = "comfortable" | "tight" | "impossible" | "unknown";
export function recipeMemoryFit(detail: LibraryRecipeDetail): {fit: RecipeMemoryFit; groupsEvaluated: number; bestHeadroomBytes?: number} {
  const groups = detail.placement.flatMap(placement => [...placement.recommendations, ...placement.rejected_groups]);
  const headrooms = groups.flatMap(group => group.nodes.length ? [Math.min(...group.nodes.map(node => node.memory_free_after_bytes))] : []);
  if (!headrooms.length) return {fit: "unknown", groupsEvaluated: groups.length};
  const best = Math.max(...headrooms);
  return {bestHeadroomBytes: best, groupsEvaluated: headrooms.length, fit: best >= 8 * 1024 ** 3 ? "comfortable" : best >= 0 ? "tight" : detail.placement.every(item => item.search_complete) ? "impossible" : "unknown"};
}
export function LibraryRecipeFit({detail}: {detail: LibraryRecipeDetail}) {
  const fit = recipeMemoryFit(detail);
  const title = detail.model_documents[0]?.model_document.identity.model.title ?? "Model metadata unavailable";
  return <section className="recipe-fit-strip" aria-label="Model and memory fit"><div><span>Models in Recipe</span><strong>{detail.model_documents.length}</strong><small>{title}{detail.model_documents.length > 1 ? ` + ${detail.model_documents.length - 1} more` : ""}</small></div><div><span>Model bytes</span><strong>{formatBytes(detail.model_documents.reduce((sum, item) => sum + item.model_document.files.reduce((bytes, file) => bytes + file.size_bytes, 0), 0))}</strong><small>Owned by the exact Model manifests</small></div><div><span>Memory fit</span><strong>{fit.fit[0]!.toUpperCase() + fit.fit.slice(1)}</strong><small>{fit.bestHeadroomBytes === undefined ? "No bounded placement evidence yet." : `${formatBytes(fit.bestHeadroomBytes)} best headroom across ${fit.groupsEvaluated} complete groups.`}</small></div></section>;
}
