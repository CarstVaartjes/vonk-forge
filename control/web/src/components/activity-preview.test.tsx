import { render, screen } from "@testing-library/react";
import { ActivityPreview } from "./activity-preview";
import type { ControlApi, VisualFleetNode } from "../api/types";

const NOW = new Date("2026-11-12T13:00:00Z");
const nodes = [
  { id: "spk_a", display_name: "spark-3542", hostname: "h", labels: {} },
] as unknown as VisualFleetNode[];

function operation(id: string, state: string, createdAt: string) {
  return { id, kind: "run_switch", state, created_at: createdAt, node_ids: ["spk_a"] };
}
const apiWith = (operations: unknown[]) =>
  ({
    operations: async () => ({ operations, total: operations.length, next_cursor: null }),
  }) as unknown as ControlApi;

test("shows the newest seven activities in the Activity page's words, newest first", async () => {
  const rows = Array.from({ length: 9 }, (_, i) =>
    operation(`op-${i}`, "succeeded", `2026-11-12T0${i}:00:00Z`),
  );
  rows.push(operation("op-fail", "failed", "2026-11-12T12:30:00Z"));
  render(<ActivityPreview api={apiWith(rows)} nodes={nodes} now={NOW} />);
  expect(await screen.findAllByRole("listitem")).toHaveLength(7);
  expect(screen.getAllByText("Run Switch · Completed").length).toBe(6);
  expect(screen.getByText("Run Switch · Failed")).toBeInTheDocument();
  expect(screen.getByText("Unsuccessful")).toBeInTheDocument();
  expect(screen.getAllByText("spark-3542")).toHaveLength(7);
  expect(screen.getByRole("link", { name: "View all" })).toHaveAttribute("href", "/activity");
});

test("says so when there is no activity, and when it cannot be read", async () => {
  const { unmount } = render(<ActivityPreview api={apiWith([])} nodes={nodes} now={NOW} />);
  expect(await screen.findByText("No activity yet.")).toBeInTheDocument();
  unmount();
  const failing = { operations: () => Promise.reject(new Error("down")) } as unknown as ControlApi;
  render(<ActivityPreview api={failing} nodes={nodes} now={NOW} />);
  expect(await screen.findByText("Activity is not available right now.")).toBeInTheDocument();
});
