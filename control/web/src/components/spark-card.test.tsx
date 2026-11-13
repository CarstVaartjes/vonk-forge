import { render, screen } from "@testing-library/react";
import { SparkCard } from "./spark-card";
import type { VisualFleetNode } from "../api/types";

const GIB = 1024 ** 3;
const gib = (value: number) => Math.round(value * GIB);
const NOW = new Date("2026-11-12T12:00:00Z");
const api = { metricsSeries: () => Promise.reject(new Error("no history")) };

function spark(over: Record<string, unknown> = {}, sample: Record<string, unknown> = {}) {
  return {
    id: "spk_a",
    display_name: "spark-3542",
    hostname: "spark-3542",
    labels: {},
    connection: { online_state: "online", last_seen_at: "2026-11-12T11:59:58Z" },
    inventory: null,
    telemetry: {
      freshness: "live",
      age_seconds: 0,
      sample: {
        observed_at: "2026-11-12T11:59:58Z",
        memory_total_bytes: gib(123.2),
        memory_available_bytes: gib(10.4),
        gpu_process_memory_bytes: gib(105.9),
        system_memory_bytes: gib(6.9),
        gpu_utilization_percent: 97,
        gpu_temperature_c: 69,
        gpu_power_watts: 38.04,
        gpu_clock: { graphics_mhz: 2200 },
        disk_free_bytes: gib(412.3),
        disk_total_bytes: gib(3686.4),
        uptime_seconds: 20 * 86400 + 21 * 3600 + 60,
        ...sample,
      },
    },
    installed: [],
    loaded: [],
    warnings: [],
    ...over,
  } as unknown as VisualFleetNode;
}

test("an online Spark reads its memory split, live readings and uptime", () => {
  render(<SparkCard api={api} node={spark()} now={NOW} index={0} />);
  expect(screen.getByRole("meter", { name: "spark-3542 unified memory" })).toHaveAttribute(
    "aria-valuetext",
    "Models 105.9 GiB, System 6.9 GiB, Free 10.4 GiB of 123.2 GiB",
  );
  expect(screen.getByText("up 20 d 21 h")).toBeInTheDocument();
  expect(screen.getByText("97")).toBeInTheDocument();
  expect(screen.getByText("38.0")).toBeInTheDocument();
  expect(screen.getByText("2200")).toBeInTheDocument();
});

test("a Spark that reports no power or uptime says Not reported instead of a value", () => {
  render(
    <SparkCard
      api={api}
      node={spark({}, { gpu_power_watts: null, uptime_seconds: null })}
      now={NOW}
      index={1}
    />,
  );
  expect(screen.getByText("Uptime: Not reported")).toBeInTheDocument();
  expect(screen.getByText("GPU power").nextSibling).toHaveTextContent("Not reported");
});

test("an offline Spark draws no traces", () => {
  const offline = spark({
    connection: { online_state: "offline", offline_reason: "stale", last_seen_at: null },
  });
  render(<SparkCard api={api} node={offline} now={NOW} index={0} />);
  expect(screen.getByText("offline")).toBeInTheDocument();
  expect(screen.queryByText("GPU temperature")).toBeNull();
});

test("a stale sample reads Not reported on every tile, as the table does", () => {
  const stale = spark();
  (stale.telemetry as { freshness: string }).freshness = "stale";
  render(<SparkCard api={api} node={stale} now={NOW} index={0} />);
  expect(screen.getByText("Uptime: Not reported")).toBeInTheDocument();
  for (const title of ["GPU", "GPU temperature", "GPU power", "GPU clock"])
    expect(screen.getByText(title).nextSibling).toHaveTextContent("Not reported");
});
