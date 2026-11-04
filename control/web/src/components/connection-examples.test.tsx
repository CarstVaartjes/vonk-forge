import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { ConnectionExamples, PER_REQUEST_NOTE } from "./connection-examples";

// The CLI test asserts the same fixture, so the two renderers cannot drift.
const fixture: {
  note: string;
  cases: { api_base: string; alias: string; setup: string; curl: string; python: string }[];
} = JSON.parse(
  readFileSync(resolve(process.cwd(), "../../tests/fixtures/connection-examples.json"), "utf8"),
);

test.each(fixture.cases)("renders the shared example text for $alias", (item) => {
  render(<ConnectionExamples apiBase={item.api_base} alias={item.alias} />);
  expect(screen.getByLabelText("setup example").textContent).toBe(item.setup);
  expect(screen.getByLabelText("curl example").textContent).toBe(item.curl);
  expect(screen.getByLabelText("Python example").textContent).toBe(item.python);
  expect(PER_REQUEST_NOTE).toBe(fixture.note);
});
