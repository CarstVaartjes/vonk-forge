import { canonicalRecipeSelector, type LibraryViewRecipe } from "../api/types";

export type LibraryRoute =
  | { kind: "root" }
  | { kind: "model"; modelKey: string }
  | { kind: "recipe"; selector: string };

function decode(value: string): string | undefined {
  try {
    return decodeURIComponent(value);
  } catch {
    return undefined;
  }
}

export function libraryRoute(path: string): LibraryRoute {
  if (path === "/library" || path === "/library/") return { kind: "root" };
  const model = /^\/library\/models\/([^/]+)$/.exec(path);
  if (model) {
    const modelKey = decode(model[1]);
    if (modelKey !== undefined && modelKey !== "~unlinked") return { kind: "model", modelKey };
  }
  const recipe = /^\/library\/recipes\/([^/]+)$/.exec(path);
  if (recipe) {
    const selector = decode(recipe[1]);
    if (selector !== undefined) return { kind: "recipe", selector };
  }
  return { kind: "root" };
}

export function modelKey(model: {
  publisher: string;
  slug: string;
  content_sha256: string;
}): string {
  return `${model.publisher}/${model.slug}@${model.content_sha256}`;
}

export function modelLibraryPath(modelKey: string): string {
  return `/library/models/${encodeURIComponent(modelKey)}`;
}

/**
 * A recipe page: by selector for browsing (the newest revision), or by revision for a running workload,
 * so the page opens the exact revision that runs. Never a recipe id.
 */
export function recipeLibraryPath(
  recipe:
    | Pick<LibraryViewRecipe, "publisher" | "slug">
    | { selector: string }
    | { revision: string },
): string {
  const target =
    "revision" in recipe
      ? recipe.revision
      : "selector" in recipe
        ? recipe.selector
        : canonicalRecipeSelector(recipe);
  return `/library/recipes/${encodeURIComponent(target)}`;
}
