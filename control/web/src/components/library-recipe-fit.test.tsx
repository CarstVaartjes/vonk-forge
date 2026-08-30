import {render, screen, within} from "@testing-library/react";
import type {LibraryRecipeDetail, PublicRecipe} from "../api/types";
import {fullLibraryDetail} from "../test-fixtures/library";
import {LibraryRecipeFit, recipeMemoryFit} from "./library-recipe-fit";

const GIB = 1024 ** 3;

function withHeadroom(headroomBytes: number, searchComplete = true): LibraryRecipeDetail {
  return {
    ...fullLibraryDetail,
    placement: fullLibraryDetail.placement.map(placement => ({
      ...placement,
      search_complete: searchComplete,
      recommendations: placement.recommendations.map(group => ({
        ...group,
        nodes: group.nodes.map(node => ({...node, memory_free_after_bytes: headroomBytes})),
      })),
      rejected_groups: placement.rejected_groups.map(group => ({
        ...group,
        nodes: group.nodes.map(node => ({...node, memory_free_after_bytes: headroomBytes - GIB})),
      })),
    })),
  };
}

const catalogRecipe = {
  model_title: "Qwen 3 family",
  model_version_title: "Qwen 3 NVFP4",
  quantizations: ["NVFP4"],
  precision: "FP4",
  maximum_runtime_memory_bytes_per_node: 72 * GIB,
} as PublicRecipe;

test.each([
  {headroom: 16 * GIB, expected: "comfortable"},
  {headroom: 4 * GIB, expected: "tight"},
  {headroom: -GIB, expected: "impossible"},
] as const)("classifies $expected fit from server-authored placement headroom", ({expected, headroom}) => {
  expect(recipeMemoryFit(withHeadroom(headroom))).toMatchObject({fit: expected});
});

test("keeps a bounded non-fit unknown when the placement search is incomplete", () => {
  expect(recipeMemoryFit(withHeadroom(-GIB, false))).toMatchObject({fit: "unknown"});
});

test("shows model family, exact variant badges, and a textual pre-install memory verdict", () => {
  render(<LibraryRecipeFit catalogRecipe={catalogRecipe} detail={withHeadroom(16 * GIB)}/>);

  const strip = screen.getByRole("region", {name: "Model variant and memory fit"});
  expect(within(strip).getByText("Qwen 3 family")).toBeVisible();
  expect(within(strip).getByText("Qwen 3 NVFP4")).toBeVisible();
  expect(within(strip).getByLabelText("Variant: NVFP4, 2 Sparks, Pair")).toBeVisible();
  expect(within(strip).getByText("Comfortable")).toHaveClass("status-pill-healthy");
  expect(within(strip).getByText(/16.0 GiB per Spark after reservations/)).toBeVisible();
  expect(within(strip).getByText(/Install and load reviews remain authoritative/)).toBeVisible();
});

test("renders an explicit no-evidence state rather than guessing fit", () => {
  render(<LibraryRecipeFit detail={{...fullLibraryDetail, placement: []}}/>);

  const strip = screen.getByRole("region", {name: "Model variant and memory fit"});
  expect(within(strip).getByText("Not measured")).toBeVisible();
  expect(within(strip).getByText(/has not produced bounded per-Spark memory evidence/)).toBeVisible();
});
