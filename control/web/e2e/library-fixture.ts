import type { Page } from "@playwright/test";
import { modelLibraryPath } from "../src/lib/library-route";
import { currentRecipeDetail, modelLibrary, recipeLibrary } from "../src/test-fixtures/library";

// The same typed Library the unit tests use, served through the real client.
export const fixtureRecipeTitle: string = currentRecipeDetail.identity.title;

// The Model page of the first Recipe's Model: it lists that Model's Recipes with their cache actions.
const model = modelLibrary.models.find(
  (entry) => entry.selector === currentRecipeDetail.model_selectors[0],
)!;
export const fixtureModelPath: string = modelLibraryPath(
  `${model.identity.publisher}/${model.identity.slug}@${model.identity.content_sha256}`,
);

export async function serveLibrary(page: Page) {
  await page.route("**/api/model/library**", (route) => route.fulfill({ json: modelLibrary }));
  await page.route("**/api/recipe/library**", (route) => route.fulfill({ json: recipeLibrary }));
  await page.route(
    (url) =>
      url.pathname === `/api/recipe/${encodeURIComponent(currentRecipeDetail.selector)}` ||
      url.pathname === `/api/recipe/${currentRecipeDetail.selector}`,
    (route) => route.fulfill({ json: currentRecipeDetail }),
  );
}
