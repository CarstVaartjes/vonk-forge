import type {LibraryRecipeSummary} from "../api/types";

export function RecipeSummary({recipe}: {recipe: LibraryRecipeSummary}) {
  return <article className="recipe-card" aria-label={`${recipe.title} Recipe`}><header><h3>{recipe.title}</h3><span>{recipe.topology_name ?? "Topology not declared"}</span></header><p>{recipe.description}</p><dl><div><dt>Capabilities</dt><dd>{recipe.capabilities.join(" · ") || "Not declared"}</dd></div><div><dt>Models</dt><dd>{recipe.recipe_document.models.length}</dd></div><div><dt>Files</dt><dd>{recipe.recipe_document.models.reduce((sum, model) => sum + model.files.length, 0)}</dd></div><div><dt>Release</dt><dd>{recipe.recipe_document.release.version}</dd></div></dl><a href={`/library/recipes/${encodeURIComponent(recipe.recipe_id)}`}>Open Recipe</a></article>;
}
