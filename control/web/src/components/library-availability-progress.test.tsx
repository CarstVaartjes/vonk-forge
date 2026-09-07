import {render, screen} from "@testing-library/react";
import {expect, test} from "vitest";
import {availabilityProgress, LibraryAvailabilityProgress} from "./library-availability-progress";

test("keeps byte progress and rate separate from file counts", () => {
  render(<LibraryAvailabilityProgress progress={availabilityProgress({
    phase: "downloading",
    completed_bytes: 42_000_000,
    total_bytes: 168_000_000,
    bytes_per_second: 87_000_000,
    eta_seconds: 24 * 60,
  })}/>);
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "42000000");
  expect(screen.getByText(/40\.1 MiB/)).toBeInTheDocument();
  expect(screen.getByText(/83\.0 MiB\/s/)).toBeInTheDocument();
  expect(screen.getByText(/24m left/)).toBeInTheDocument();
});

test("uses an indeterminate track when the total is unknown", () => {
  render(<LibraryAvailabilityProgress progress={availabilityProgress({phase: "build", completed_bytes: 8, step: "Compiling attention kernels", log_excerpt: "step 8"})}/>);
  expect(screen.getByRole("progressbar")).toHaveClass("is-indeterminate");
  expect(screen.getByText("Compiling attention kernels")).toBeInTheDocument();
  expect(screen.getByText("8 B received")).toBeInTheDocument();
});
