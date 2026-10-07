import {addWire, compareWire, type WireNumber} from "../api/contract-numeric";
import type {LibraryViewRecipeDetail} from "../api/types";
import {formatBytes} from "../lib/fleet";
import {selectedRecipeFiles} from "./library-recipe-files";

export type RecipeMemoryFit = "comfortable" | "tight" | "impossible" | "unknown";
export function recipeMemoryFit(detail: LibraryViewRecipeDetail): {fit: RecipeMemoryFit; groupsEvaluated: number; bestHeadroomBytes?: WireNumber} {
  const groups = detail.placement.flatMap(placement => [...placement.recommendations, ...placement.rejected_groups]);
  const headrooms = groups.flatMap(group => group.nodes.length ? [group.nodes.map(node => node.memory_free_after_bytes).reduce((minimum, value) => compareWire(value, minimum) < 0 ? value : minimum)] : []);
  if (!headrooms.length) return {fit: "unknown", groupsEvaluated: groups.length};
  const best = headrooms.reduce((maximum, value) => compareWire(value, maximum) > 0 ? value : maximum);
  return {bestHeadroomBytes: best, groupsEvaluated: headrooms.length, fit: compareWire(best, 8 * 1024 ** 3) >= 0 ? "comfortable" : compareWire(best, 0) >= 0 ? "tight" : detail.placement.every(item => item.search_complete) ? "impossible" : "unknown"};
}
export function LibraryRecipeFit({detail}: {detail: LibraryViewRecipeDetail}) {
  const fit = recipeMemoryFit(detail);
  const title = detail.model_documents[0]?.model_document.identity.model.title ?? "Model metadata unavailable";
  const selected = selectedRecipeFiles(detail.model_documents);
  const modelBytes = selected.unresolved.length ? "Unknown" : formatBytes(selected.files.reduce<WireNumber>((sum, file) => addWire(sum, file.size_bytes), 0));
  return <section className="recipe-fit-strip" aria-label="Model and memory fit"><div><span>Models in Recipe</span><strong>{detail.model_documents.length}</strong><small>{title}{detail.model_documents.length > 1 ? ` + ${detail.model_documents.length - 1} more` : ""}</small></div><div><span>Model bytes</span><strong>{modelBytes}</strong><small>{selected.unresolved.length ? "Recipe Model file selection is incomplete." : "Selected files from the exact Model manifests"}</small></div><div><span>Memory fit</span><strong>{fit.fit[0]!.toUpperCase() + fit.fit.slice(1)}</strong><small>{fit.bestHeadroomBytes === undefined ? "No bounded placement evidence yet." : `${formatBytes(fit.bestHeadroomBytes)} best headroom across ${fit.groupsEvaluated} complete groups.`}</small></div></section>;
}
