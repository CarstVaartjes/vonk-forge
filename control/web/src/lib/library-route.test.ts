import { expect, test } from "vitest";
import { libraryRoute, recipeLibraryPath } from "./library-route";

test("a recipe link resolves to the selector the detail page sends to the API", () => {
  const revisionId = "b8bf29f4-0000-4000-8000-000000000001";
  expect(libraryRoute(recipeLibraryPath(revisionId))).toEqual({
    kind: "recipe",
    recipeId: revisionId,
  });
  expect(libraryRoute(recipeLibraryPath("vonk-forge/glm-sglang-dual"))).toEqual({
    kind: "recipe",
    recipeId: "vonk-forge/glm-sglang-dual",
  });
});
