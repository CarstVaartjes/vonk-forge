import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { AreaChart } from "./area-chart";
import { ArcGauge } from "./arc-gauge";
import { StackedArea } from "./stacked-area";
import { linearScale, niceTicks } from "./scale";
import { Sparkline } from "./sparkline";

const format = (value: number) => `${value} W`;
const base = { title: "GPU power", label: "GPU power, last hour", start: 0, end: 100, format };
const a = {
  key: "a",
  name: "Kitchen",
  color: "var(--series-1)",
  points: [
    { t: 0, v: 10 },
    { t: 40, v: 20 },
    { t: 50, v: null },
    { t: 70, v: 25 },
    { t: 100, v: 30 },
  ],
};

test("ticks are clean and the scale maps the domain onto the range", () => {
  expect(niceTicks(0, 87)).toEqual([0, 25, 50, 75, 100]);
  expect(linearScale([0, 10], [100, 0])(2.5)).toBe(75);
});

test("a missing sample leaves a gap: the line and the wash are each two pieces and never cross it", () => {
  const { container } = render(<AreaChart {...base} series={[a]} />);
  expect(container.querySelector("path.chart-line")?.getAttribute("d")?.match(/M/g)).toHaveLength(
    2,
  );
  const wash = container.querySelector("path[fill^='url']")?.getAttribute("d") ?? "";
  expect(wash.match(/Z/g)).toHaveLength(2);
  // The second piece starts where its own data starts (t=70), not at the left edge of the plot.
  expect(wash.split("Z")[1]).toMatch(/^M(?!50 )/);
  expect(screen.getByRole("img", { name: "GPU power, last hour" })).toBeVisible();
});

test("an empty range says so and draws nothing", () => {
  const { container } = render(<AreaChart {...base} series={[{ ...a, points: [] }]} />);
  expect(screen.getByText("No samples in this range")).toBeVisible();
  expect(container.querySelector("svg")).toBeNull();
});

test("the pointer and the arrow keys move a crosshair with a tooltip; the table lists the same values", () => {
  render(<AreaChart {...base} series={[a]} />);
  const chart = screen.getByRole("img");
  chart.getBoundingClientRect = () => ({ left: 0, width: 600 }) as DOMRect;
  fireEvent.pointerMove(chart, { clientX: 580 });
  expect(screen.getByRole("tooltip")).toHaveTextContent("Kitchen: 30 W");
  fireEvent.keyDown(chart, { key: "ArrowLeft" });
  expect(screen.getByRole("tooltip")).toHaveTextContent("Kitchen: 25 W");
  fireEvent.keyDown(chart, { key: "Escape" });
  expect(screen.queryByRole("tooltip")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Show as table" }));
  expect(screen.getByRole("table", { name: "GPU power" })).toHaveTextContent("30 W");
});

test("a sparkline places points by time and with nothing to show renders nothing", () => {
  const { container, rerender } = render(
    <Sparkline
      start={0}
      end={100}
      label="trace"
      points={[
        { t: 50, v: 1 },
        { t: 100, v: 2 },
      ]}
    />,
  );
  // t=50 of 0..100 sits halfway across the 114 px plot, not at the left edge.
  expect(container.querySelector("path.chart-line")?.getAttribute("d")).toMatch(/^M57\.0 /);
  rerender(<Sparkline start={0} end={100} label="trace" points={[{ t: 0, v: null }]} />);
  expect(container.querySelector("svg")).toBeNull();
});

test("a gauge is a meter with its value and caption, and an absent reading draws no arc", () => {
  const { container, rerender } = render(
    <ArcGauge
      title="Temperature"
      value={69}
      unit="°C"
      max={100}
      captions={["warns at 85 °C"]}
      color="red"
      ticks={[{ value: 85, tone: "warn" }]}
    />,
  );
  const meter = screen.getByRole("meter", { name: "Temperature" });
  expect(meter).toHaveAttribute("aria-valuetext", "69 °C, warns at 85 °C");
  expect(container.querySelectorAll(".arc-tick")).toHaveLength(1);
  rerender(<ArcGauge title="Temperature" value={null} unit="°C" max={100} color="red" />);
  expect(screen.queryByRole("meter")).toBeNull();
  expect(screen.getByText("Not reported")).toBeVisible();
  expect(container.querySelector("svg")).toBeNull();
});

test("stacked layers break at a point with a missing layer", () => {
  const layers = [
    { key: "a", color: "red" },
    { key: "b", color: "blue" },
  ];
  const points = [
    { t: 0, values: [1, 1] },
    { t: 10, values: [2, 1] },
    { t: 20, values: [null, 1] },
    { t: 30, values: [2, 2] },
    { t: 40, values: [3, 2] },
  ];
  const { container } = render(
    <StackedArea
      points={points}
      layers={layers}
      start={0}
      end={40}
      max={6}
      unit="GiB"
      name="Memory"
    />,
  );
  // Two runs per layer: no area crosses the missing point.
  expect(container.querySelectorAll("path")[0]?.getAttribute("d")?.match(/M/g)).toHaveLength(2);
  const empty = render(
    <StackedArea
      points={[{ t: 0, values: [null, null] }]}
      layers={layers}
      start={0}
      end={40}
      max={6}
      unit="GiB"
      name="Memory"
      empty={<p>None</p>}
    />,
  );
  expect(empty.getByText("None")).toBeVisible();
});

test("charts that share a crosshair follow one cursor time, each at its own nearest sample", () => {
  const b = {
    ...a,
    key: "b",
    name: "Garage",
    points: [
      { t: 5, v: 7 },
      { t: 95, v: 8 },
    ],
  };
  const Pair = () => {
    const [at, setAt] = useState<number | null>(null);
    const crosshair = { at, onChange: setAt };
    return (
      <>
        <AreaChart {...base} title="One" label="one" series={[a]} crosshair={crosshair} />
        <AreaChart {...base} title="Two" label="two" series={[b]} crosshair={crosshair} />
      </>
    );
  };
  render(<Pair />);
  const [one] = screen.getAllByRole("img");
  (one as Element).getBoundingClientRect = () => ({ left: 0, width: 600 }) as DOMRect;
  fireEvent.pointerMove(one as Element, { clientX: 580 });
  const tips = screen.getAllByRole("tooltip");
  expect(tips[0]).toHaveTextContent("Kitchen: 30 W");
  expect(tips[1]).toHaveTextContent("Garage: 8 W");
});
