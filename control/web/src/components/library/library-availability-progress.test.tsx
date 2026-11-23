import { parseContractJson, materialize } from "../../api/contract-numeric";
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { availabilityProgress, LibraryAvailabilityProgress } from "./library-availability-progress";

test("keeps byte progress and rate separate from file counts", () => {
  render(
    <LibraryAvailabilityProgress
      progress={availabilityProgress({
        phase: "downloading",
        completed_bytes: 42_000_000,
        total_bytes: 168_000_000,
        smoothed_bytes_per_second: 87_000_000,
        eta_seconds: 24 * 60,
      })}
    />,
  );
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "25");
  expect(screen.getByText(/40\.1 MiB/)).toBeInTheDocument();
  expect(screen.getByText(/83\.0 MiB\/s/)).toBeInTheDocument();
  expect(screen.getByText(/24m left/)).toBeInTheDocument();
});

test("uses an indeterminate track when the total is unknown", () => {
  render(
    <LibraryAvailabilityProgress
      progress={availabilityProgress({
        phase: "build",
        completed_bytes: 8,
        step: "Compiling attention kernels",
        log_excerpt: "step 8",
      })}
    />,
  );
  expect(screen.getByRole("progressbar")).toHaveClass("is-indeterminate");
  expect(screen.getByText("Compiling attention kernels")).toBeInTheDocument();
  expect(screen.getByText("8 B received")).toBeInTheDocument();
});

test("verification clears transfer ETA and keeps known transfer counters indeterminate", () => {
  render(
    <LibraryAvailabilityProgress
      progress={availabilityProgress({
        phase: "verifying",
        completed_bytes: 100,
        total_bytes: 100,
        smoothed_bytes_per_second: 50,
        eta_seconds: 2,
        completed_items: 2,
        total_items: 3,
        activity: "active",
      })}
    />,
  );
  expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  expect(screen.queryByText(/left/)).not.toBeInTheDocument();
  expect(screen.getByText("2 of 3 items")).toBeInTheDocument();
});

test("advisory stall and last observation are visible without presenting stale speed", () => {
  render(
    <LibraryAvailabilityProgress
      progress={availabilityProgress({
        phase: "copying",
        completed_bytes: 10,
        smoothed_bytes_per_second: 50,
        eta_seconds: 2,
        activity: "possibly_stalled",
        last_progress_at: "2026-09-08T12:00:00Z",
      })}
    />,
  );
  expect(screen.getByText("Possibly stalled · work continues")).toBeInTheDocument();
  expect(screen.getByText(/Last progress/).querySelector("time")).toHaveAttribute(
    "dateTime",
    "2026-09-08T12:00:00Z",
  );
  expect(screen.queryByText(/B\/s/)).not.toBeInTheDocument();
});

test("displays exact item counts and a bounded accessible wide-byte ratio", () => {
  const progress = availabilityProgress(
    materialize(
      parseContractJson(
        '{"phase":"copying","completed_bytes":9223372036854775807,"total_bytes":18446744073709551615,"completed_items":9007199254740993,"total_items":9007199254740994}',
      ),
    ),
  );
  render(<LibraryAvailabilityProgress progress={progress} />);
  expect(screen.getByText("9007199254740993 of 9007199254740994 items")).toBeInTheDocument();
  const bar = screen.getByRole("progressbar");
  expect(bar).toHaveAttribute("aria-valuemax", "100");
  expect(Number(bar.getAttribute("aria-valuenow"))).toBeCloseTo(50);
});

test("missing measured bytes stay unknown instead of inventing zero progress", () => {
  render(<LibraryAvailabilityProgress progress={availabilityProgress({ phase: "copying" })} />);
  expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  expect(screen.getByText("Not reported received")).toBeInTheDocument();
});

test("shows the phase in plain words and one bar per Spark from the server fields", () => {
  render(
    <LibraryAvailabilityProgress
      progress={availabilityProgress({
        phase: "transfer",
        completed_bytes: 50,
        total_bytes: 200,
        total_bytes_known: true,
        smoothed_bytes_per_second: 1024,
        eta_seconds: 90,
        members: [
          {
            member_id: "spark-a",
            phase: "transfer",
            completed_bytes: 40,
            total_bytes: 100,
            smoothed_bytes_per_second: 2048,
            eta_seconds: 30,
          },
          { member_id: "spark-b", phase: "transfer", completed_bytes: 10, total_bytes: 100 },
        ],
      })}
    />,
  );
  expect(screen.getByText("Copying model files to the Sparks")).toBeInTheDocument();
  const bars = screen.getAllByRole("progressbar");
  expect(bars).toHaveLength(3);
  expect(bars[0]).toHaveAttribute("aria-valuenow", "25");
  expect(bars[1]).toHaveAttribute("aria-valuenow", "40");
  expect(bars[2]).toHaveAttribute("aria-valuenow", "10");
  expect(screen.getByText(/2\.0 KiB\/s/)).toBeInTheDocument();
  expect(screen.getAllByText(/left/)).toHaveLength(2);
});

test("a rate field the Controller does not send is not invented", () => {
  render(
    <LibraryAvailabilityProgress
      progress={availabilityProgress({
        phase: "downloading",
        completed_bytes: 10,
        total_bytes: 100,
        total_bytes_known: true,
        bytes_per_second: 50,
        rate: 50,
      })}
    />,
  );
  expect(screen.queryByText(/\/s/)).not.toBeInTheDocument();
});
