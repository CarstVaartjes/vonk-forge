import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ControlApi, FleetProfile } from "../../api/types";
import { connectionSnippets } from "../connection-examples";
import { ServingNow } from "./serving-now";

const endpoint = {
  alias: "qwen-code",
  api_base: "https://vonk-forge.example.ts.net/v1",
  backend_api_base: "http://192.168.1.211:8888/v1",
  generation: 3,
  node_id: "spk_00000000000000000000000000000001",
  observed_at: "2026-09-10T00:00:00Z",
  plan_digest: "c".repeat(64),
};
const published = {
  assignment_id: "33333333-3333-4333-8333-333333333333",
  recipe_title: "Qwen Code",
  desired_state: "running",
  alias: "qwen-code",
  state: "published",
  endpoint,
};
const loaded = { number: 2, status: "loaded" } as unknown as FleetProfile;

function apiWith(profiles: unknown[], assignments: unknown[]) {
  return {
    profiles: vi.fn(async () => ({ generated_at: "2026-09-10T00:00:00Z", profiles })),
    profileEndpoints: vi.fn(async () => ({
      number: 2,
      observed_at: "2026-09-10T00:00:00Z",
      assignments,
    })),
  } as unknown as Pick<ControlApi, "profiles" | "profileEndpoints">;
}

test("offers the profile page's connection example and never calls the gateway", async () => {
  // Break caught: the example is rebuilt differently from the Library's, or the browser calls the
  // inference gateway itself.
  const fetched = vi.spyOn(globalThis, "fetch");
  render(<ServingNow api={apiWith([loaded], [published])} refreshKey="a" pollKey="1" />);

  const line = await screen.findByRole("listitem");
  await userEvent.click(within(line).getByText("Show connection example"));
  expect(within(line).getByLabelText("curl example").textContent).toBe(
    connectionSnippets(endpoint.api_base, "qwen-code").curl,
  );
  expect(fetched).not.toHaveBeenCalled();
});

test("shows nothing when no profile is loaded", async () => {
  // Break caught: an empty "Serving now" heading appears on a fleet that serves nothing.
  const api = apiWith([{ number: 1, status: "saved" }], []);
  render(<ServingNow api={api} refreshKey="a" pollKey="1" />);

  await vi.waitFor(() => expect(api.profiles).toHaveBeenCalled());
  expect(screen.queryByRole("heading", { name: "Serving now" })).not.toBeInTheDocument();
  expect(api.profileEndpoints).not.toHaveBeenCalled();
});

test("a failed read says so instead of pretending nothing is served", async () => {
  // Break caught: an unreachable endpoint view reads as "nothing is serving".
  const api = apiWith([loaded], []);
  vi.mocked(api.profileEndpoints).mockRejectedValue(new Error("down"));
  render(<ServingNow api={api} refreshKey="a" pollKey="1" />);

  expect(await screen.findByRole("status")).toHaveTextContent("not available");
});

test("reads again on the next poll while nothing is shown, but not once something is", async () => {
  // Break caught: a profile that finishes loading never appears until a workload changes, or every
  // poll re-reads the profile and endpoint views.
  const api = apiWith([loaded], []);
  const { rerender } = render(<ServingNow api={api} refreshKey="a" pollKey="1" />);
  await vi.waitFor(() => expect(api.profileEndpoints).toHaveBeenCalledTimes(1));

  vi.mocked(api.profileEndpoints).mockResolvedValue({
    number: 2,
    observed_at: "2026-09-10T00:00:00Z",
    assignments: [published],
  } as never);
  rerender(<ServingNow api={api} refreshKey="a" pollKey="2" />);
  expect(await screen.findByRole("listitem")).toBeVisible();
  expect(api.profileEndpoints).toHaveBeenCalledTimes(2);

  rerender(<ServingNow api={api} refreshKey="a" pollKey="3" />);
  rerender(<ServingNow api={api} refreshKey="a" pollKey="4" />);
  expect(api.profileEndpoints).toHaveBeenCalledTimes(2);
  rerender(<ServingNow api={api} refreshKey="b" pollKey="5" />);
  await vi.waitFor(() => expect(api.profileEndpoints).toHaveBeenCalledTimes(3));
});
