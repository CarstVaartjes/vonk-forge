import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ControlApi, FleetProfile } from "../api/types";
import { connectionSnippets } from "./connection-examples";
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

test("lists each published alias of the loaded profile with the profile page's connection example", async () => {
  // Break caught: the example is rebuilt differently from the Library's, an unpublished alias is
  // offered, or the browser calls the inference gateway itself.
  const fetched = vi.spyOn(globalThis, "fetch");
  const notPublished = {
    ...published,
    assignment_id: "44444444-4444-4444-8444-444444444444",
    alias: "draft",
    state: "not-published-yet",
    endpoint: null,
  };
  render(<ServingNow api={apiWith([loaded], [published, notPublished])} refreshKey="a" />);

  const line = await screen.findByRole("listitem");
  expect(line).toHaveTextContent("qwen-code · Qwen Code · published");
  expect(screen.queryByText("draft")).not.toBeInTheDocument();
  await userEvent.click(within(line).getByText("Show connection example"));
  expect(within(line).getByLabelText("curl example").textContent).toBe(
    connectionSnippets(endpoint.api_base, "qwen-code").curl,
  );
  expect(fetched).not.toHaveBeenCalled();
});

test("shows nothing when no profile is loaded", async () => {
  // Break caught: an empty "Serving now" heading appears on a fleet that serves nothing.
  const api = apiWith([{ number: 1, status: "draft" }], []);
  render(<ServingNow api={api} refreshKey="a" />);

  await vi.waitFor(() => expect(api.profiles).toHaveBeenCalled());
  expect(screen.queryByRole("heading", { name: "Serving now" })).not.toBeInTheDocument();
  expect(api.profileEndpoints).not.toHaveBeenCalled();
});

test("a failed read says so instead of pretending nothing is served", async () => {
  // Break caught: an unreachable endpoint view reads as "nothing is serving".
  const api = apiWith([loaded], []);
  vi.mocked(api.profileEndpoints).mockRejectedValue(new Error("down"));
  render(<ServingNow api={api} refreshKey="a" />);

  expect(await screen.findByRole("status")).toHaveTextContent("not available");
});
