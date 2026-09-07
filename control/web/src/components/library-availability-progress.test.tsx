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

test("verification clears transfer ETA and keeps known transfer counters indeterminate", () => {
  render(<LibraryAvailabilityProgress progress={availabilityProgress({phase: "verifying", completed_bytes: 100, total_bytes: 100, bytes_per_second: 50, eta_seconds: 2, completed_items: 2, total_items: 3, activity: "active"})}/>);
  expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  expect(screen.queryByText(/left/)).not.toBeInTheDocument();
  expect(screen.getByText("2 of 3 items")).toBeInTheDocument();
});

test("advisory stall and last observation are visible without presenting stale speed", () => {
  render(<LibraryAvailabilityProgress progress={availabilityProgress({phase: "copying", completed_bytes: 10, bytes_per_second: 50, eta_seconds: 2, activity: "possibly_stalled", last_progress_at: "2026-09-08T12:00:00Z"})}/>);
  expect(screen.getByText("Possibly stalled · work continues")).toBeInTheDocument();
  expect(screen.getByText(/Last progress/).querySelector("time")).toHaveAttribute("dateTime", "2026-09-08T12:00:00Z");
  expect(screen.queryByText(/B\/s/)).not.toBeInTheDocument();
});
