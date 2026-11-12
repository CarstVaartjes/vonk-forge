import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { render, screen } from "@testing-library/react";
import type { CatalogSyncStatus, ControlApi, PlatformObservation } from "../api/types";
import { VersionLine } from "./version-line";

// `vonkctl` renders the same fixture and prints the same lines, so the two clients cannot drift.
const fixture: {
  cases: {
    name: string;
    platform: { source_sha: string | null };
    sync: Record<string, unknown> | null;
    lines: string[];
  }[];
} = JSON.parse(
  readFileSync(resolve(process.cwd(), "../../tests/fixtures/version-line.json"), "utf8"),
);

type VersionApi = Pick<ControlApi, "platformObservation" | "catalogSyncStatus">;

function api(
  platformObservation: () => Promise<PlatformObservation>,
  catalogSyncStatus: () => Promise<CatalogSyncStatus | null>,
) {
  return { platformObservation, catalogSyncStatus } as unknown as VersionApi;
}

function facts(line: HTMLElement) {
  return Array.from(line.children).map((child) => child.textContent);
}

test.each(fixture.cases)("shows the shared lines: $name", async (item) => {
  render(
    <VersionLine
      api={api(
        async () => ({ api: item.platform }) as PlatformObservation,
        async () => item.sync as CatalogSyncStatus | null,
      )}
    />,
  );

  expect(facts(await screen.findByLabelText("Versions"))).toEqual(item.lines);
});

test("a failed read says Not reported while the other facts still show", async () => {
  // Break caught: a failed platform read hides the library facts, or the footer renders blank values.
  const synced = fixture.cases[0];
  render(
    <VersionLine
      api={api(
        async () => {
          throw new Error("platform unavailable");
        },
        async () => synced.sync as CatalogSyncStatus,
      )}
    />,
  );

  expect(facts(await screen.findByLabelText("Versions"))).toEqual([
    "Controller build: Not reported",
    ...synced.lines.slice(1),
  ]);
});

test("reads again when the key changes, so the footer is not frozen at mount", async () => {
  // Break caught: the footer keeps the values read at first mount after the operator navigates.
  const synced = fixture.cases[0];
  let build = "a".repeat(40);
  const versions = api(
    async () => ({ api: { source_sha: build } }) as PlatformObservation,
    async () => synced.sync as CatalogSyncStatus,
  );
  const { rerender } = render(<VersionLine api={versions} readKey="/fleet" />);
  expect(await screen.findByText("aaaaaaaaaaaa")).toBeVisible();

  build = "b".repeat(40);
  rerender(<VersionLine api={versions} readKey="/library" />);
  expect(await screen.findByText("bbbbbbbbbbbb")).toBeVisible();
});
