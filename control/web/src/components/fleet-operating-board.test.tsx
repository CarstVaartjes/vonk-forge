import {render, screen} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {vi} from "vitest";
import type {ControlApi, FleetProfile, FleetProfileApplicationView, FleetProfilePreview} from "../api/types";
import {FleetOperatingBoard} from "./fleet-operating-board";

const profile = {number: 2, name: "Coding", description: "Code on the fleet", status: "ready", assignments: [], fleet: [], warnings: [], next_actions: []} as unknown as FleetProfile;
const preview = {allowed: true, steps: [{index: 0, kind: "start", label: "Start Coding", node_ids: []}], reasons: []} as unknown as FleetProfilePreview;
const application = {state: "running", progress: {child_progress: {phase: "start", node_ids: []}}, status_reason: null} as unknown as FleetProfileApplicationView;

test("loads a numbered profile through preview, load, and profile progress", async () => {
  const user = userEvent.setup();
  const api = {profiles: vi.fn(async () => ({profiles: [profile]})), previewProfile: vi.fn(async () => preview), loadProfile: vi.fn(async () => application), profileProgress: vi.fn(async () => ({...application, state: "succeeded"}))} as unknown as ControlApi;
  render(<FleetOperatingBoard api={api}/>);

  expect(await screen.findByText("Profile 2 · Coding")).toBeVisible();
  const load = await screen.findByRole("button", {name: "Load profile"});
  await user.click(load);
  expect(api.loadProfile).toHaveBeenCalledWith(2, {dry_run: false, request_key: expect.stringMatching(/^[0-9a-f-]{36}$/)});
  expect(await screen.findByRole("region", {name: "Profile load progress"})).toBeVisible();
});
