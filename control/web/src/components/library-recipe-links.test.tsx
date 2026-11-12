import { render, screen, within } from "@testing-library/react";
import { fullLibraryDetail } from "../test-fixtures/library";
import { LibraryRecipeAuthority } from "./library-recipe-detail";
import { LibraryRecipeLinks } from "./library-recipe-links";

test("links the creator's page on the public site and shows each licence as information", () => {
  // Break caught: the link points at another address or leaks the referrer, or the licence
  // is missing or turns into a control that could gate use.
  render(<LibraryRecipeLinks detail={fullLibraryDetail} />);

  const region = screen.getByRole("region", { name: "Creator, licence and source" });
  const { publisher, slug } = fullLibraryDetail.recipe;
  const link = within(region).getByRole("link", { name: "Creator and details on vonkforge.ai" });
  expect(link).toHaveAttribute("href", `https://vonkforge.ai/recipes/${publisher}/${slug}`);
  expect(link).toHaveAttribute("rel", "noreferrer");
  const license = fullLibraryDetail.model_documents[0]!.model_display!.license;
  expect(region).toHaveTextContent(`licence: ${license.spdx}`);
  expect(within(region).queryByRole("button")).not.toBeInTheDocument();
});

test("a licence address that is not a web address is not a link", () => {
  // Break caught: published text such as a javascript: address becomes a clickable href.
  const detail = structuredClone(fullLibraryDetail);
  for (const item of detail.model_documents) item.model_display!.license.url = "javascript:alert(1)";
  render(<LibraryRecipeLinks detail={detail} />);

  const hrefs = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
  expect(hrefs.some((href) => href?.startsWith("javascript:"))).toBe(false);
  expect(hrefs).toContain(`https://vonkforge.ai/recipes/${detail.recipe.publisher}/${detail.recipe.slug}`);
});

test("the recipe detail lists no per-file bytes", () => {
  // Break caught: file paths, sizes and digests come back into the recipe detail outside the cache view.
  render(<LibraryRecipeAuthority api={{} as never} detail={fullLibraryDetail} />);

  expect(screen.queryByText(/sha256:/)).not.toBeInTheDocument();
});
