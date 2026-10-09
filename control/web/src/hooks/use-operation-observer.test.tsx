import { act, render, screen } from "@testing-library/react";
import { vi } from "vitest";
import { LifecycleState } from "../api/vocabulary.generated";
import { ApiError, ContractViolation } from "../api/errors";
import { backoffDelay, useOperationObserver } from "./use-operation-observer";

type Op = { state: typeof LifecycleState.RUNNING | typeof LifecycleState.SUCCEEDED };
function Probe({ fetch, onTerminal }: { fetch: () => Promise<Op>; onTerminal?: () => void }) {
  const observer = useOperationObserver<Op>({
    isTerminal: (op) => op.state === LifecycleState.SUCCEEDED,
    onTerminal,
  });
  return (
    <>
      <button onClick={() => observer.start({ state: LifecycleState.RUNNING }, fetch)}>
        start
      </button>
      <span data-testid="state">{observer.value?.state}</span>
      <span data-testid="conn">{observer.connection}</span>
      <span data-testid="bg">{String(observer.background)}</span>
      <span data-testid="observing">{String(observer.observing)}</span>
      <span data-testid="fatal">{observer.fatal}</span>
    </>
  );
}
const go = async (ms: number) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test("backoff is bounded", () => {
  expect(backoffDelay(0)).toBe(1_000);
  expect(backoffDelay(3)).toBe(8_000);
  expect(backoffDelay(50)).toBe(15_000);
});

test("a temporary disconnection resumes and completes, calling onTerminal once", async () => {
  const fetch = vi
    .fn<() => Promise<Op>>()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValue({ state: LifecycleState.SUCCEEDED });
  const onTerminal = vi.fn();
  render(<Probe fetch={fetch} onTerminal={onTerminal} />);
  act(() => screen.getByText("start").click());
  await go(1_000);
  expect(screen.getByTestId("conn")).toHaveTextContent("reconnecting");
  expect(screen.getByTestId("state")).toHaveTextContent("running");
  await go(20_000);
  expect(screen.getByTestId("conn")).toHaveTextContent("live");
  expect(screen.getByTestId("state")).toHaveTextContent("succeeded");
  expect(onTerminal).toHaveBeenCalledTimes(1);
});

test("the online event retries immediately instead of waiting out the backoff", async () => {
  const fetch = vi.fn<() => Promise<Op>>();
  for (let i = 0; i < 5; i++) fetch.mockRejectedValueOnce(new TypeError("offline"));
  fetch.mockResolvedValue({ state: LifecycleState.SUCCEEDED });
  render(<Probe fetch={fetch} />);
  act(() => screen.getByText("start").click());
  await go(10_000);
  const calls = fetch.mock.calls.length;
  await act(async () => {
    window.dispatchEvent(new Event("online"));
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(fetch.mock.calls.length).toBe(calls + 1);
});

test("unreadable replies preserve the accepted operation and recover", async () => {
  const fetch = vi
    .fn<() => Promise<Op>>()
    .mockRejectedValueOnce(new ContractViolation("GET", "/api/operations/accepted", 200))
    .mockResolvedValue({ state: LifecycleState.SUCCEEDED });
  render(<Probe fetch={fetch} />);
  act(() => screen.getByText("start").click());
  await go(1_000);
  expect(screen.getByTestId("state")).toHaveTextContent("running");
  expect(screen.getByTestId("fatal")).toBeEmptyDOMElement();
  await go(2_000);
  expect(screen.getByTestId("state")).toHaveTextContent("succeeded");
});

test("observation expires without changing remote state and a fresh session completes", async () => {
  const fetch = vi.fn<() => Promise<Op>>().mockRejectedValue(new TypeError("unreadable"));
  render(<Probe fetch={fetch} />);
  act(() => screen.getByText("start").click());
  await go(180_000);
  const calls = fetch.mock.calls.length;
  expect(screen.getByTestId("observing")).toHaveTextContent("false");
  expect(screen.getByTestId("state")).toHaveTextContent("running");
  await go(180_000);
  expect(fetch).toHaveBeenCalledTimes(calls);
  fetch.mockResolvedValue({ state: LifecycleState.SUCCEEDED });
  act(() => screen.getByText("start").click());
  await go(1_000);
  expect(screen.getByTestId("state")).toHaveTextContent("succeeded");
});

test("a hung fetch is aborted at the deadline and does not retain the observer", async () => {
  let signal: AbortSignal | undefined;
  const fetch = (value?: AbortSignal): Promise<Op> => {
    signal = value;
    return new Promise(() => {});
  };
  render(<Probe fetch={fetch} />);
  act(() => screen.getByText("start").click());
  await go(180_000);
  expect(signal?.aborted).toBe(true);
  expect(screen.getByTestId("observing")).toHaveTextContent("false");
  expect(screen.getByTestId("state")).toHaveTextContent("running");
});

test("denied observation surfaces authority without changing the remote outcome", async () => {
  const fetch = vi.fn<() => Promise<Op>>().mockRejectedValue(new ApiError(403, "Access denied"));
  render(<Probe fetch={fetch} />);
  act(() => screen.getByText("start").click());
  await go(180_000);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("state")).toHaveTextContent("running");
  expect(screen.getByTestId("fatal")).toHaveTextContent("Access denied");
  fetch.mockResolvedValue({ state: LifecycleState.SUCCEEDED });
  act(() => screen.getByText("start").click());
  await go(1_000);
  expect(screen.getByTestId("state")).toHaveTextContent("succeeded");
});
