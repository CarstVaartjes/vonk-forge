import {render, screen} from "@testing-library/react";
import {LibraryRecipeAuthority} from "./library-recipe-detail";
import {minimalLibraryDetail} from "../test-fixtures/library";

test("renders ordered model inputs for an exact Recipe", () => {
  render(<LibraryRecipeAuthority api={{} as never} detail={minimalLibraryDetail}/>);
  expect(screen.getByRole("heading", {name: "Ordered Model inputs"})).toBeVisible();
  expect(screen.getAllByText(minimalLibraryDetail.model_documents[0]!.model_document.identity.model.title).length).toBeGreaterThan(0);
});
