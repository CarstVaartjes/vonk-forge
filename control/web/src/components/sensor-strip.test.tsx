import { render, screen } from "@testing-library/react";
import type { VisualFleetNode } from "../api/types";
import { SensorStrip } from "./sensor-strip";

const node = (sample: object | null) =>
  ({
    id: "spk_1",
    display_name: "Kitchen",
    telemetry: sample && { freshness: "live", age_seconds: 1, sample },
  }) as unknown as VisualFleetNode;
const draw = (sample: object | null) =>
  render(<SensorStrip node={node(sample)} points={[]} now={new Date()} color="red" />);

test("only the sensors the Spark reports get a tile, with the figure as reported", () => {
  draw({ board_temperature_max_c: 61, fan_rpm_max: 3210, nvme_temperature_c: null });
  expect(screen.getByText("Hottest board sensor").parentElement).toHaveTextContent("61 °C");
  expect(screen.getByText("Fastest fan").parentElement).toHaveTextContent("3210 RPM");
  expect(screen.queryByText("NVMe")).toBeNull();
  expect(screen.queryByText("Network card")).toBeNull();
});

test("a Spark that is not reporting live says Not reported and draws no tile", () => {
  draw(null);
  expect(screen.getByText("Not reported")).toBeVisible();
  expect(screen.queryByText("Hottest board sensor")).toBeNull();
});
