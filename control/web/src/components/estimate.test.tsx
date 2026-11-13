import { render, screen } from "@testing-library/react";
import { Estimate, EstimateTag } from "./estimate";

test("the approximation sign and the est. tag appear exactly for estimated values", () => {
  const { rerender } = render(
    <Estimate basis="estimated">
      <span>42 tok/s</span> <EstimateTag basis="estimated" />
    </Estimate>,
  );
  expect(screen.getByText("≈")).toBeVisible();
  expect(screen.getByText("est.")).toBeVisible();
  rerender(
    <Estimate basis="measured">
      <span>42 tok/s</span> <EstimateTag basis="measured" />
    </Estimate>,
  );
  expect(screen.queryByText("≈")).toBeNull();
  expect(screen.queryByText("est.")).toBeNull();
});
