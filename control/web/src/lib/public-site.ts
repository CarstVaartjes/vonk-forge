/** The public site's page for one recipe; `vonkctl recipe detail` prints the same address. */
export function recipePublicUrl(recipe: { publisher: string; slug: string }): string {
  return `https://vonkforge.ai/recipes/${encodeURIComponent(recipe.publisher)}/${encodeURIComponent(recipe.slug)}`;
}

/** Published text may carry any string; only web addresses become links. */
export function webAddress(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}
