import { render, screen } from "@testing-library/react";
import type { CatalogSyncStatus, ControlApi, PlatformObservation } from "../api/types";
import { VersionLine } from "./version-line";

const platform = { api: { source_sha: "0123456789abcdef0123456789abcdef01234567" } } as PlatformObservation;
const sync = {
  library_version: "1.4.0",
  completed_at: "2026-11-12T08:00:00Z",
  created_at: "2026-11-12T07:59:00Z",
} as CatalogSyncStatus;

function api(platformObservation: () => Promise<PlatformObservation>, catalogSyncStatus: () => Promise<CatalogSyncStatus | null>) {
  return { platformObservation, catalogSyncStatus } as unknown as Pick<ControlApi, "platformObservation" | "catalogSyncStatus">;
}

test("shows the build, library version and last sync from the API", async () => {
  // Break caught: the line shows a constant or a guess instead of the API's own values.
  render(<VersionLine api={api(async () => platform, async () => sync)} />);

  const line = await screen.findByLabelText("Versions");
  expect(line).toHaveTextContent("Controller build 0123456789ab");
  expect(line).toHaveTextContent("Library version 1.4.0");
  expect(line.querySelector("time")).toHaveAttribute("datetime", "2026-11-12T08:00:00Z");
});

test("one failed fact reads Not reported while the others still show", async () => {
  // Break caught: a failed platform read hides the library facts, or the footer renders blank values.
  render(
    <VersionLine
      api={api(
        async () => {
          throw new Error("platform unavailable");
        },
        async () => sync,
      )}
    />,
  );

  const line = await screen.findByLabelText("Versions");
  expect(line).toHaveTextContent("Controller build Not reported");
  expect(line).toHaveTextContent("Library version 1.4.0");
});

test("a library that never synced says so", async () => {
  render(<VersionLine api={api(async () => platform, async () => null)} />);

  const line = await screen.findByLabelText("Versions");
  expect(line).toHaveTextContent("Library version Not reported");
  expect(line).toHaveTextContent("Last sync none yet");
});
