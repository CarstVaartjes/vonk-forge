/** The public site's origin; the Documentation link and recipe pages share it. */
export const PUBLIC_SITE_ORIGIN = "https://vonkforge.ai";

/** The public site's page for one recipe; `vonkctl recipe detail` prints the same address. */
export function recipePublicUrl(recipe: { publisher: string; slug: string }): string {
  return `${PUBLIC_SITE_ORIGIN}/recipes/${encodeURIComponent(recipe.publisher)}/${encodeURIComponent(recipe.slug)}`;
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
