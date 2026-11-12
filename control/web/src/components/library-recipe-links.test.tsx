import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen, within } from "@testing-library/react";
import { fullLibraryDetail } from "../test-fixtures/library";
import { LibraryRecipeAuthority } from "./library-recipe-detail";
import { LibraryRecipeLinks } from "./library-recipe-links";

// `vonkctl recipe detail` prints the same fixture's lines, so the two clients cannot drift.
const fixture: {
  cases: {
    name: string;
    publisher: string;
    slug: string;
    source_reference: string | null;
    models: { model_title: string; license: Record<string, unknown> }[];
    lines: string[];
  }[];
} = JSON.parse(
  readFileSync(resolve(process.cwd(), "../../tests/fixtures/recipe-links.json"), "utf8"),
);

function detailFor(item: (typeof fixture.cases)[number]) {
  const detail = structuredClone(fullLibraryDetail);
  detail.recipe.publisher = item.publisher;
  detail.recipe.slug = item.slug;
  detail.definition.provenance.source_reference = item.source_reference;
  const first = detail.model_documents[0]!;
  detail.model_documents = item.models.map((model, index) => ({
    ...structuredClone(first),
    model_document: {
      ...first.model_document,
      identity: { ...first.model_document.identity, slug: `model-${index}` },
    },
    model_display: { ...first.model_display!, ...model } as never,
  }));
  return detail;
}

test.each(fixture.cases)("shows the shared lines: $name", (item) => {
  render(<LibraryRecipeLinks detail={detailFor(item)} />);

  const region = screen.getByRole("region", { name: "Creator, licence and source" });
  const lines = within(region)
    .getAllByRole("listitem")
    .map((line) => line.textContent);
  expect(lines).toEqual(item.lines);
});

test("web addresses are links that leave no referrer, and nothing here is a control", () => {
  // Break caught: the link points at another address or leaks the referrer, or the licence
  // turns into a control that could gate use.
  render(<LibraryRecipeLinks detail={detailFor(fixture.cases[0]!)} />);

  const region = screen.getByRole("region", { name: "Creator, licence and source" });
  const link = within(region).getByRole("link", { name: "https://vonkforge.ai/recipes/vonk/tiny" });
  expect(link).toHaveAttribute("href", "https://vonkforge.ai/recipes/vonk/tiny");
  expect(link).toHaveAttribute("rel", "noreferrer");
  expect(within(region).queryByRole("button")).not.toBeInTheDocument();
});

test("a licence address that is not a web address is not a link", () => {
  // Break caught: published text such as a javascript: address becomes a clickable href.
  const unsafe = fixture.cases.find((item) => item.name.includes("not a web address"))!;
  render(<LibraryRecipeLinks detail={detailFor(unsafe)} />);

  const hrefs = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
  expect(hrefs.some((href) => href?.startsWith("javascript:"))).toBe(false);
});

test("the recipe detail lists no per-file bytes", () => {
  // Break caught: file paths, sizes and digests come back into the recipe detail outside the cache view.
  render(<LibraryRecipeAuthority api={{} as never} detail={fullLibraryDetail} />);

  expect(screen.queryByText(/sha256:/)).not.toBeInTheDocument();
});
