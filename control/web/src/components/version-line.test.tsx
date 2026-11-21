import { render, screen } from "@testing-library/react";
import type { CatalogSyncStatus, ControlApi, PlatformObservation } from "../api/types";
import { VersionLine } from "./version-line";

const synced = {
  state: "current",
  library_version: "1.4.0",
  completed_at: "2026-11-12T08:00:00Z",
  created_at: "2026-11-12T07:59:00Z",
} as unknown as CatalogSyncStatus;

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

test("a failed platform read leaves the other facts showing", async () => {
  // Break caught: a failed platform read hides the library facts, or the footer renders blank values.
  render(
    <VersionLine
      api={api(
        async () => {
          throw new Error("platform unavailable");
        },
        async () => synced,
      )}
    />,
  );

  const shown = facts(await screen.findByLabelText("Versions"));
  expect(shown).toHaveLength(3);
  expect(shown.every((fact) => fact?.trim())).toBe(true);
  expect(shown.some((fact) => fact?.includes("1.4.0"))).toBe(true);
});

test("reads again when the key changes, so the footer is not frozen at mount", async () => {
  // Break caught: the footer keeps the values read at first mount after the operator navigates.
  let build = "a".repeat(40);
  const versions = api(
    async () => ({ api: { source_sha: build } }) as PlatformObservation,
    async () => synced,
  );
  const { rerender } = render(<VersionLine api={versions} readKey="/fleet" />);
  expect(await screen.findByText("aaaaaaaaaaaa")).toBeVisible();

  build = "b".repeat(40);
  rerender(<VersionLine api={versions} readKey="/library" />);
  expect(await screen.findByText("bbbbbbbbbbbb")).toBeVisible();
});
