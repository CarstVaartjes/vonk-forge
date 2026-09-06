import {render, screen} from "@testing-library/react";
import {App} from "../app";
import {librarySnapshot} from "../test-fixtures/library";
import type {ControlApi} from "../api/types";

test("renders the canonical model and recipe workcell", async () => {
  history.replaceState(null, "", "/library");
  render(<App api={{librarySnapshot: async () => librarySnapshot} as unknown as ControlApi}/>);
  expect(await screen.findByRole("heading", {name: "Library"})).toBeVisible();
  expect(screen.getByLabelText("Models")).toBeVisible();
  expect(screen.getByLabelText("Recipes matching selected Model")).toBeVisible();
});
test("keeps the exact model filter addressable", async () => {
  history.replaceState(null, "", "/library?view=models");
  render(<App api={{librarySnapshot: async () => librarySnapshot} as unknown as ControlApi}/>);
  expect(await screen.findByRole("heading", {name: "Models"})).toBeVisible();
  expect(screen.getByRole("combobox", {name: "Filter exact model"})).toBeVisible();
});
