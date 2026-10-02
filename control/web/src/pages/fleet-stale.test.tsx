import {act, render, screen} from "@testing-library/react";
import {vi} from "vitest";
import type {ControlApi} from "../api/types";
import {ToastProvider} from "../components/toast";
import {FleetPage} from "./fleet-simplified";

const node = {
  id: `spk_${"a".repeat(32)}`, display_name: "Spark A", hostname: "a.local", labels: {},
  connection: {online_state: "online", offline_reason: null, last_seen_at: null},
  inventory: {disk_free_bytes: 1, disk_total_bytes: 2, host_memory_free_bytes: 1, host_memory_total_bytes: 2},
  telemetry: null, installed: [], loaded: [], warnings: [],
};
const snapshot = {event_cursor: 1, nodes: [node]};

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

test("a failed background refresh keeps the last snapshot and says it is disconnected and how old it is", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("EventSource", undefined);
  const visualFleet = vi.fn().mockResolvedValueOnce(snapshot).mockRejectedValue(new TypeError("Failed to fetch"));
  render(<ToastProvider><FleetPage api={{visualFleet} as unknown as ControlApi}/></ToastProvider>);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByText("Online")).toBeVisible();
  expect(screen.queryByText("disconnected")).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(25_000); });
  expect(screen.getByText("disconnected")).toBeVisible();
  expect(screen.getByText(/Showing the last known state of the Fleet, updated \d+ s ago/)).toBeVisible();
  expect(screen.getByText("Spark A")).toBeVisible();
});
