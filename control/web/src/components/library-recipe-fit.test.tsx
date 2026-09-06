import {render, screen} from "@testing-library/react";
import {fullLibraryDetail} from "../test-fixtures/library";
import {LibraryRecipeFit, recipeMemoryFit} from "./library-recipe-fit";
import {LibraryRecipeVisual} from "./library-recipe-visual";
import {selectedRecipeFiles} from "./library-recipe-files";

test("reports unknown fit without placement evidence", () => {
  expect(recipeMemoryFit(fullLibraryDetail).fit).toBe("unknown");
  render(<LibraryRecipeFit detail={fullLibraryDetail}/>);
  expect(screen.getByRole("region", {name: "Model and memory fit"})).toHaveTextContent("Unknown");
});

test("counts only selected files across multiple Models with colliding paths", () => {
  const detail = structuredClone(fullLibraryDetail);
  const first = structuredClone(detail.model_documents[0]!);
  const second = structuredClone(detail.model_documents[0]!);
  const fileA = {id: "a", path: "weights/shared.bin", roles: ["weights"], sha256: "a".repeat(64), size_bytes: 10};
  const fileB = {id: "b", path: "weights/shared.bin", roles: ["weights"], sha256: "b".repeat(64), size_bytes: 20};
  const ignored = {id: "ignored", path: "weights/ignored.bin", roles: ["weights"], sha256: "c".repeat(64), size_bytes: 999};
  first.model_document.files = [fileA];
  first.selection.files = [{file_id: "a", id: "mount-a", mount: {read_only: true, target: "/models/a"}, roles: ["weights"]}];
  second.model_document.files = [fileB, ignored];
  second.selection.files = [{file_id: "b", id: "mount-b", mount: {read_only: true, target: "/models/b"}, roles: ["weights"]}];
  detail.model_documents = [first, second];
  const selected = selectedRecipeFiles(detail.model_documents);
  expect(selected.unresolved).toEqual([]);
  expect(selected.files.map(file => file.size_bytes)).toEqual([10, 20]);
  render(<LibraryRecipeFit detail={detail}/>);
  expect(screen.getByRole("region", {name: "Model and memory fit"})).toHaveTextContent("30 B");
  render(<LibraryRecipeVisual document={detail.definition} modelDocuments={detail.model_documents}/>);
  expect(screen.getByRole("region", {name: "Recipe contract"})).toHaveTextContent(/Selected Model files\s*2 · 30 B/);
});
