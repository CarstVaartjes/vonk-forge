import {render, screen} from "@testing-library/react";
import {Sparkline, sparklinePath} from "./sparkline";

test("builds bounded deterministic SVG geometry", () => {
  // Break caught: invalid or unbounded coordinates make the chart disappear or overflow.
  expect(sparklinePath([0, 50, 100], 100, 30)).toBe("M 2 28 L 50 15 L 98 2");
  expect(sparklinePath([null, 50, null, 100], 100, 30)).toBe("M 34 28 M 98 2");
  expect(sparklinePath([null, null], 100, 30)).toBe("");
});

test("exposes an accessible chart and visible text summary", () => {
  render(<Sparkline
    label="GPU utilization history"
    values={[10, null, 30, 20]}
    formatValue={value => `${Math.round(value)}%`}
  />);

  const chart = screen.getByRole("img", {name: "GPU utilization history"});
  expect(chart).toHaveAttribute("viewBox", "0 0 100 30");
  expect(chart).toHaveAccessibleDescription("Latest 20%; range 10% to 30%; 3 reported samples.");
  expect(screen.getByText("Latest 20% · Range 10%–30% · 3 samples")).toBeVisible();
  expect(chart.querySelector("path")).toHaveAttribute("aria-hidden", "true");
});

test("weights rollup means by reported metric count", () => {
  render(<Sparkline
    label="GPU utilization history"
    values={[]}
    sampleLabel="buckets"
    series={[
      {minimum: 10, mean: 10, maximum: 10, count: 1},
      {minimum: 30, mean: 30, maximum: 30, count: 3},
    ]}
    formatValue={value => `${Math.round(value)}%`}
  />);

  expect(screen.getByRole("img", {name: "GPU utilization history"})).toHaveAccessibleDescription("Mean 25%; latest mean 30%; reported range 10% to 30%; 2 reported buckets.");
  expect(screen.getByText("Mean 25% · Range 10%–30% · 2 reported buckets")).toBeVisible();
});

test("states when no finite samples were reported", () => {
  render(<Sparkline label="Temperature history" values={[null, Number.NaN]}/>);

  expect(screen.getByRole("img", {name: "Temperature history"})).toHaveAccessibleDescription("No reported samples.");
  expect(screen.getByText("No reported samples")).toBeVisible();
  expect(document.querySelector("path")).not.toBeInTheDocument();
});
