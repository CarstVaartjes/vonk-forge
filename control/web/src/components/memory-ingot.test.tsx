import { render, screen } from "@testing-library/react";
import { MemoryIngot } from "./memory-ingot";
import { memoryLevel, nodeMemorySplit, nodeVitals } from "../lib/fleet";
import type { VisualFleetNode } from "../api/types";

const GIB = 1024 ** 3;
const gib = (value: number) => Math.round(value * GIB);

/** The D12 capture: 123.2 GiB, models 105.9, system 6.9, free 10.4. */
function sparkWith(sample: Record<string, number | null>): VisualFleetNode {
  return {
    connection: { online_state: "online" },
    telemetry: {
      freshness: "live",
      sample: { memory_total_bytes: gib(123.2), memory_available_bytes: gib(10.4), ...sample },
    },
  } as unknown as VisualFleetNode;
}

const d12 = sparkWith({ gpu_process_memory_bytes: gib(105.9), system_memory_bytes: gib(6.9) });

test("the D12 capture shows Models, System and Free in the legend and the meter name", () => {
  render(<MemoryIngot label="spark-1 unified memory" memory={nodeMemorySplit(d12)} />);
  expect(screen.getByRole("meter", { name: "spark-1 unified memory" })).toHaveAttribute(
    "aria-valuetext",
    "Models 105.9 GiB, System 6.9 GiB, Free 10.4 GiB of 123.2 GiB",
  );
  expect(screen.getByText("105.9 GiB")).toBeInTheDocument();
  expect(screen.queryByText(/free$/)).toBeNull();
});

test("without the split the bar is Used and Free", () => {
  render(<MemoryIngot label="m" memory={nodeMemorySplit(sparkWith({}))} />);
  expect(screen.getByRole("meter")).toHaveAttribute(
    "aria-valuetext",
    "Used 112.8 GiB, Free 10.4 GiB of 123.2 GiB",
  );
});

test("memory warns on free memory: 7.9 GiB warns, 3.9 GiB is serious, 8 GiB is calm", () => {
  expect(memoryLevel(gib(8))).toBe("ok");
  expect(memoryLevel(gib(7.9))).toBe("warn");
  expect(memoryLevel(gib(3.9))).toBe("serious");
  const low = sparkWith({ memory_available_bytes: gib(7.9) });
  expect(nodeVitals(low).memory.amber).toBe(true);
  expect(nodeVitals(d12).memory.amber).toBe(false);
  render(<MemoryIngot label="m" memory={nodeMemorySplit(low)} />);
  expect(screen.getByText("Under 8 GiB free")).toBeInTheDocument();
});

test("a Spark with no memory reading says Not reported", () => {
  render(<MemoryIngot label="m" memory={null} />);
  expect(screen.getByText("Not reported")).toBeInTheDocument();
});
