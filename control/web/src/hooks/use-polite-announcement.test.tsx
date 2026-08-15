import {act, render, screen} from "@testing-library/react";
import {usePoliteAnnouncement} from "./use-polite-announcement";

function Probe({message}: {message: string}) {
  return <span role="status">{usePoliteAnnouncement(message, 5_000)}</span>;
}

afterEach(() => vi.useRealTimers());

test("announces the first summary then coalesces rapid updates", () => {
  vi.useFakeTimers();
  const view = render(<Probe message="1 live, 1 stale"/>);
  expect(screen.getByRole("status")).toHaveTextContent("1 live, 1 stale");

  view.rerender(<Probe message="2 live, 0 stale"/>);
  view.rerender(<Probe message="3 live, 0 stale"/>);
  expect(screen.getByRole("status")).toHaveTextContent("1 live, 1 stale");
  act(() => vi.advanceTimersByTime(4_999));
  expect(screen.getByRole("status")).toHaveTextContent("1 live, 1 stale");
  act(() => vi.advanceTimersByTime(1));
  expect(screen.getByRole("status")).toHaveTextContent("3 live, 0 stale");

  view.unmount();
  expect(vi.getTimerCount()).toBe(0);
});

test("announces the first non-empty summary immediately after loading", () => {
  vi.useFakeTimers();
  const view = render(<Probe message=""/>);

  view.rerender(<Probe message="2 live, 0 stale"/>);

  expect(screen.getByRole("status")).toHaveTextContent("2 live, 0 stale");
});
