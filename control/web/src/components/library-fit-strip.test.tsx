import { render, screen } from "@testing-library/react";
import type { VisualFleetSnapshot } from "../api/types";
import { LibraryFitStrip } from "./library-fit-strip";

const GIB = 1024 ** 3;
const node = (id: string) => ({
  id,
  connection: { online_state: "online" },
  telemetry: {
    freshness: "live",
    sample: {
      memory_total_bytes: 123 * GIB,
      memory_available_bytes: 10 * GIB,
      gpu_process_memory_bytes: 100 * GIB,
      system_memory_bytes: 13 * GIB,
    },
  },
});
const fleet = { nodes: [node("a"), node("b")] } as unknown as VisualFleetSnapshot;

test("the strip says how many Sparks, their memory in total and what is free", () => {
  render(<LibraryFitStrip fleet={fleet} failed={false} />);
  expect(screen.getByText("2 Sparks")).toBeInTheDocument();
  expect(screen.getByText("246.0 GiB unified memory")).toBeInTheDocument();
  expect(screen.getByText("20.0 GiB unified memory free")).toBeInTheDocument();
  expect(screen.getByRole("meter", { name: "Fleet unified memory" })).toBeInTheDocument();
});

test("a failed read says Not reported and draws no bar; a read still loading draws nothing", () => {
  const { container, rerender } = render(<LibraryFitStrip fleet={undefined} failed />);
  expect(screen.getAllByText("Not reported")).toHaveLength(3);
  expect(screen.queryByRole("meter")).toBeNull();
  rerender(<LibraryFitStrip fleet={undefined} failed={false} />);
  expect(container).toBeEmptyDOMElement();
});
