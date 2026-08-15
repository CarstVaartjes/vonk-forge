export type LibraryRoute =
  | {kind: "root"}
  | {kind: "model"; family: string; unlinked: boolean}
  | {kind: "recipe"; recipeId: string};

const UNLINKED_SEGMENT = "~unlinked";

function decode(value: string): string | undefined {
  try { return decodeURIComponent(value); } catch { return undefined; }
}

export function libraryRoute(path: string): LibraryRoute {
  if (path === "/library" || path === "/library/") return {kind: "root"};
  const model = /^\/library\/models\/([^/]+)$/.exec(path);
  if (model) {
    if (model[1] === UNLINKED_SEGMENT) return {kind: "model", family: "", unlinked: true};
    const family = decode(model[1]);
    if (family !== undefined) return {kind: "model", family, unlinked: false};
  }
  const recipe = /^\/library\/recipes\/([^/]+)$/.exec(path);
  if (recipe) {
    const recipeId = decode(recipe[1]);
    if (recipeId !== undefined) return {kind: "recipe", recipeId};
  }
  return {kind: "root"};
}

export function modelLibraryPath(family: string): string {
  return `/library/models/${encodeURIComponent(family)}`;
}

export function unlinkedLibraryPath(): string {
  return `/library/models/${UNLINKED_SEGMENT}`;
}

export function recipeLibraryPath(recipeId: string): string {
  return `/library/recipes/${encodeURIComponent(recipeId)}`;
}
