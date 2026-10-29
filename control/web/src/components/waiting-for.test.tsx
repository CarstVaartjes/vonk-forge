import { render, screen } from "@testing-library/react";
import { WaitingFor } from "./waiting-for";

const GIB = 1024 ** 3;
const swap = {
  active_bytes: 16 * GIB,
  desired_bytes: 64 * GIB,
  used_bytes: 2 * GIB,
  held_bytes: 0,
  usable_bytes: 14 * GIB,
  rise_bytes: 35 * GIB,
  needed_setting_bytes: 40 * GIB,
};
const blocker = {
  code: "run-switch.resource.insufficient_swap" as const,
  detail: "The load waits for swap.",
  severity: "error" as const,
  node_ids: ["spark-1"],
};

test("a wait for swap names its numbers", () => {
  render(<WaitingFor blockers={[{ ...blocker, swap }]} />);
  expect(screen.getByRole("list", { name: "Waiting for" })).toHaveTextContent(
    "Swap: 16.0 GiB active (setting 64.0 GiB), 2.0 GiB in use, 0 B held by loading runs; usable 14.0 GiB; this load rises 35.0 GiB; needs a setting of at least 40.0 GiB",
  );
});

test("an unknown setting is said so, and a wait without evidence shows no swap line", () => {
  const { rerender } = render(
    <WaitingFor blockers={[{ ...blocker, swap: { ...swap, desired_bytes: null } }]} />,
  );
  expect(screen.getByText(/\(setting unknown\)/)).toBeVisible();
  rerender(<WaitingFor blockers={[blocker]} />);
  expect(screen.queryByText(/Swap:/)).toBeNull();
});
