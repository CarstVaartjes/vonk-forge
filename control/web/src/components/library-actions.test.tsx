import {fireEvent, render, screen} from "@testing-library/react";
import {vi} from "vitest";
import {LibraryRecipeAuthority} from "./library-recipe-detail";
import {minimalLibraryDetail} from "../test-fixtures/library";

test("renders ordered model inputs for an exact Recipe", () => {
  render(<LibraryRecipeAuthority api={{} as never} detail={minimalLibraryDetail}/>);
  expect(screen.getByRole("heading", {name: "Ordered Model inputs"})).toBeVisible();
  expect(screen.getAllByText(minimalLibraryDetail.model_documents[0]!.model_document.identity.model.title).length).toBeGreaterThan(0);
});

test("lists the other recipes for this model with a one-line comparison and links", () => {
  const alternatives = [
    {selector: "vonk-forge/glm-sglang-dual", title: "GLM SGLang dual", engine: "sglang", creator: "MiaAI-Lab", node_count: 2, version: "1.7.2", cache: "not_cached" as const, fits_fleet: "ready" as const},
    {selector: "vonk-forge/glm-vllm-single", title: "GLM vLLM single", engine: "vllm", creator: null, node_count: 1, version: "0.9", cache: "cached" as const, fits_fleet: "blocked" as const},
  ];
  const navigate = vi.fn();
  render(<LibraryRecipeAuthority api={{} as never} detail={{...minimalLibraryDetail, alternatives}} onNavigate={(event, path) => { event.preventDefault(); navigate(path); }}/>);
  const section = screen.getByRole("region", {name: "Other recipes for this model"});
  expect(section).toHaveTextContent("sglang · 2 Sparks · MiaAI-Lab · v1.7.2 · not cached · fits fleet");
  expect(section).toHaveTextContent("vllm · 1 Spark · unknown creator · v0.9 · cached · does not fit fleet");
  fireEvent.click(screen.getByRole("link", {name: /GLM SGLang dual/}));
  expect(navigate).toHaveBeenCalledWith("/library/recipes/vonk-forge%2Fglm-sglang-dual");
});

test("shows no alternatives section when the model has only this recipe", () => {
  render(<LibraryRecipeAuthority api={{} as never} detail={{...minimalLibraryDetail, alternatives: []}}/>);
  expect(screen.queryByRole("region", {name: "Other recipes for this model"})).toBeNull();
});
