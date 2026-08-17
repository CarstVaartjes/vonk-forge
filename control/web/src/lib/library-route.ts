export type LibraryRoute =
  | {kind: "root"}
  | {kind: "model"; modelKey: string; unlinked: boolean}
  | {kind: "recipe"; recipeId: string};

const UNLINKED_SEGMENT = "~unlinked";

function decode(value: string): string | undefined {
  try { return decodeURIComponent(value); } catch { return undefined; }
}

export function libraryRoute(path: string): LibraryRoute {
  if (path === "/library" || path === "/library/") return {kind: "root"};
  const model = /^\/library\/models\/([^/]+)$/.exec(path);
  if (model) {
    if (model[1] === UNLINKED_SEGMENT) return {kind: "model", modelKey: "", unlinked: true};
    const modelKey = decode(model[1]);
    if (modelKey !== undefined) return {kind: "model", modelKey, unlinked: false};
  }
  const recipe = /^\/library\/recipes\/([^/]+)$/.exec(path);
  if (recipe) {
    const recipeId = decode(recipe[1]);
    if (recipeId !== undefined) return {kind: "recipe", recipeId};
  }
  return {kind: "root"};
}

export function modelVersionKey(model: {publisher: string; slug: string; content_sha256: string}): string {
  return `${model.publisher}/${model.slug}@${model.content_sha256}`;
}

export function modelLibraryPath(modelKey: string): string {
  return `/library/models/${encodeURIComponent(modelKey)}`;
}

export function unlinkedLibraryPath(): string {
  return `/library/models/${UNLINKED_SEGMENT}`;
}

export function recipeLibraryPath(recipeId: string): string {
  return `/library/recipes/${encodeURIComponent(recipeId)}`;
}
