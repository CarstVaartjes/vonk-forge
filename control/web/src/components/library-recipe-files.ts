import type {LibraryRecipeDetail} from "../api/types";

export type RecipeModelFile = LibraryRecipeDetail["model_documents"][number]["model_document"]["files"][number];

export type SelectedRecipeFiles = {
  files: RecipeModelFile[];
  unresolved: string[];
};

export function selectedRecipeFiles(modelDocuments: LibraryRecipeDetail["model_documents"]): SelectedRecipeFiles {
  const files: RecipeModelFile[] = [];
  const unresolved: string[] = [];
  for (const item of modelDocuments) {
    const byId = new Map(item.model_document.files.map(file => [file.id, file]));
    for (const selection of item.selection.files) {
      const file = byId.get(selection.file_id);
      if (file) files.push(file);
      else unresolved.push(`${item.selection.id}:${selection.file_id}`);
    }
  }
  return {files, unresolved};
}
