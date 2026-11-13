import { fireEvent, render, screen } from "@testing-library/react";
import { AreaChart } from "./area-chart";
import { Columns } from "./columns";
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
    { t: 50, v: null },
    { t: 100, v: 30 },
  ],
};

test("ticks are clean and the scale maps the domain onto the range", () => {
  expect(niceTicks(0, 87)).toEqual([0, 25, 50, 75, 100]);
  expect(linearScale([0, 10], [100, 0])(2.5)).toBe(75);
});

test("a missing sample leaves a gap: the line is two pieces, not one", () => {
  const { container } = render(<AreaChart {...base} series={[a]} />);
  expect(container.querySelector("path.chart-line")?.getAttribute("d")?.match(/M/g)).toHaveLength(
    2,
  );
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
  expect(screen.getByRole("tooltip")).toHaveTextContent("Kitchen: 10 W");
  fireEvent.keyDown(chart, { key: "Escape" });
  expect(screen.queryByRole("tooltip")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Show as table" }));
  expect(screen.getByRole("table", { name: "GPU power" })).toHaveTextContent("30 W");
});

test("a sparkline and columns with nothing to show render nothing", () => {
  const { container } = render(
    <>
      <Sparkline values={[null, null]} label="x" />
      <Columns values={[]} label="y" />
    </>,
  );
  expect(container.querySelector("svg")).toBeNull();
});
