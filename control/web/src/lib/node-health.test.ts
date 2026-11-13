import type { VisualFleetNode } from "../api/types";
import { nodeHealth } from "./node-health";

const GIB = 1024 ** 3;
const NOW = new Date("2026-09-27T10:00:05Z");
const live = (sample: object) =>
  ({
    id: "spk_1",
    display_name: "Kitchen",
    warnings: [],
    loaded: [],
    installed: [],
    connection: { online_state: "online", last_seen_at: "2026-09-27T10:00:00Z" },
    telemetry: {
      freshness: "live",
      age_seconds: 1,
      sample: { observed_at: "2026-09-27T10:00:00Z", ...sample },
    },
  }) as unknown as VisualFleetNode;
const alert = (node_id: string, severity: string, summary: string) =>
  ({
    name: "A",
    severity,
    summary,
    labels: { node_id },
    active_at: "2026-09-27T09:00:00Z",
  }) as never;

test("findings come worst first, with the all-clear ones the live snapshot proves", () => {
  const node = live({
    gpu_temperature_c: 69,
    memory_total_bytes: 100 * GIB,
    memory_available_bytes: 10 * GIB,
  });
  const rows = nodeHealth(
    node,
    [node],
    NOW,
    [
      alert("spk_1", "warning", "Agent is one release behind."),
      alert("spk_2", "critical", "Other."),
    ],
    false,
  );
  expect(rows.map((row) => [row.tone, row.title, row.detail])).toEqual([
    ["warn", "Warning", "Agent is one release behind."],
    ["ok", "GPU temperature normal", "69 °C"],
    ["ok", "Memory headroom fine", "10.0 GiB free"],
  ]);
});

test("a hot GPU and low memory are rows; alerts that could not be read are said, not assumed clear", () => {
  const node = live({
    gpu_temperature_c: 91,
    memory_total_bytes: 100 * GIB,
    memory_available_bytes: 3 * GIB,
  });
  const rows = nodeHealth(node, [node], NOW, undefined, true);
  expect(rows.map((row) => [row.tone, row.title])).toEqual([
    ["bad", "GPU temperature serious"],
    ["bad", "Memory headroom low"],
    ["neutral", "Alerts unavailable"],
  ]);
});

test("a Spark that is not reporting live gets no all-clear row, only what is known", () => {
  const node = {
    ...live({}),
    telemetry: null,
    connection: { online_state: "offline", offline_reason: "stale" },
  };
  expect(nodeHealth(node as never, [], NOW, [], false).map((row) => [row.tone, row.title])).toEqual(
    [
      ["bad", "Offline"],
      ["ok", "No alerts firing"],
    ],
  );
});
