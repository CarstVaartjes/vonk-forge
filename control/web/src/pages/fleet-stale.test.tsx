vi.mock("../components/metrics-chart", () => ({ MetricsChart: () => null }));
import { act, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { ApiClient } from "../api/client";
import type { ControlApi, VisualFleetNode, VisualFleetSnapshot } from "../api/types";
import { ToastProvider } from "../components/toast";
import { FleetPage } from "./fleet-simplified";

const node: VisualFleetNode = {
  id: `spk_${"a".repeat(32)}`,
  display_name: "Spark A",
  hostname: "a.local",
  labels: {},
  lifecycle: "managed",
  connection: {
    agent_state: "active",
    certificate_state: "valid",
    online_state: "online",
    offline_reason: null,
    last_seen_at: null,
    last_seen_age_seconds: null,
  },
  inventory: null,
  telemetry: null,
  installed: [],
  loaded: [],
  warnings: [],
  reservations: {
    disk_bytes: 0,
    unified_memory_bytes: 0,
    host_memory_bytes: 0,
    gpu_memory_bytes: 0,
    port_count: 0,
  },
};
const snapshot: VisualFleetSnapshot = {
  event_cursor: 1,
  generated_at: "2026-08-15T12:00:00Z",
  authority_revision: "a".repeat(64),
  nodes: [node],
};

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("a failed background refresh keeps the last snapshot and says it is disconnected and how old it is", async () => {
  vi.useFakeTimers();
  const api: ControlApi = new ApiClient();
  const visualFleet = vi
    .spyOn(api, "visualFleet")
    .mockResolvedValueOnce(snapshot)
    .mockRejectedValue(new TypeError("Failed to fetch"));
  const connection = Object.assign(new EventTarget(), { url: "/api/fleet/stream", close: vi.fn() });
  const fleetEvents = vi.spyOn(api, "fleetEvents").mockReturnValue(connection);
  const { unmount } = render(
    <ToastProvider>
      <FleetPage api={api} />
    </ToastProvider>,
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(screen.getByText("Online")).toBeVisible();
  expect(screen.queryByText("disconnected")).toBeNull();
  expect(fleetEvents).toHaveBeenCalledOnce();
  act(() => {
    connection.dispatchEvent(new Event("error"));
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(25_000);
  });
  expect(screen.getByText("disconnected")).toBeVisible();
  expect(
    screen.getByText(/Showing the last known state of the Fleet, updated \d+ s ago/),
  ).toBeVisible();
  expect(screen.getByText("Spark A")).toBeVisible();
  expect(visualFleet).toHaveBeenCalledTimes(3);
  unmount();
  expect(connection.close).toHaveBeenCalledOnce();
});
