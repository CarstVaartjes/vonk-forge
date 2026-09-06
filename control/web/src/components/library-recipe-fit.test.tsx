import {render, screen} from "@testing-library/react";
import {fullLibraryDetail} from "../test-fixtures/library";
import {LibraryRecipeFit, recipeMemoryFit} from "./library-recipe-fit";

test("reports unknown fit without placement evidence", () => {
  expect(recipeMemoryFit(fullLibraryDetail).fit).toBe("unknown");
  render(<LibraryRecipeFit detail={fullLibraryDetail}/>);
  expect(screen.getByRole("region", {name: "Model and memory fit"})).toHaveTextContent("Unknown");
});
