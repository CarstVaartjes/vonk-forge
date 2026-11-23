import { CacheOperationKind } from "../../api/vocabulary.generated";
import type { ControlApi, LibraryViewRecipe } from "../../api/types";
import { canonicalRecipeSelector } from "../../api/types";
import { LibraryPreparation } from "./library-preparation";
import { LibraryRecipeDownloadAction } from "./library-recipe-download-action";
import { LibraryRecipeRemoveAction } from "./library-recipe-remove-action";

/**
 * What can be done with a recipe's cache, exactly as the Controller lists it:
 * Download, a live preparation to follow and cancel, or Remove. A recipe the
 * Controller says nothing can be done to shows no button.
 */
export function LibraryRecipeActions({
  api,
  recipe,
  missingModels = [],
  onChanged,
}: {
  api: ControlApi;
  recipe: Pick<LibraryViewRecipe, "publisher" | "slug" | "local" | "actions">;
  missingModels?: string[];
  onChanged(): void;
}) {
  const selector = canonicalRecipeSelector(recipe);
  const { actions, local } = recipe;
  return (
    <div className="library-recipe-actions">
      {actions.includes(CacheOperationKind.DOWNLOAD) && (
        <LibraryRecipeDownloadAction
          api={api}
          missingModels={missingModels}
          onDownloaded={onChanged}
          selector={selector}
        />
      )}
      {actions.includes(CacheOperationKind.CANCEL) && local.preparation && (
        <LibraryPreparation api={api} preparation={local.preparation} what="recipe download" />
      )}
      {actions.includes(CacheOperationKind.REMOVE) && (
        <LibraryRecipeRemoveAction api={api} onRemoved={onChanged} selector={selector} />
      )}
    </div>
  );
}
