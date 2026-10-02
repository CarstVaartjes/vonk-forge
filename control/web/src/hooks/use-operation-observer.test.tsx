import {act, render, screen} from "@testing-library/react";
import {vi} from "vitest";
import {backoffDelay, FatalObservationError, useOperationObserver} from "./use-operation-observer";

type Op = {state: "running" | "succeeded"};
function Probe({fetch, onTerminal}: {fetch: () => Promise<Op>; onTerminal?: () => void}) {
  const observer = useOperationObserver<Op>({isTerminal: op => op.state === "succeeded", onTerminal});
  return <>
    <button onClick={() => observer.start({state: "running"}, fetch)}>start</button>
    <span data-testid="state">{observer.value?.state}</span>
    <span data-testid="conn">{observer.connection}</span>
    <span data-testid="bg">{String(observer.background)}</span>
    <span data-testid="fatal">{observer.fatal}</span>
  </>;
}
const go = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

test("backoff is bounded", () => {
  expect(backoffDelay(0)).toBe(1_000);
  expect(backoffDelay(3)).toBe(8_000);
  expect(backoffDelay(50)).toBe(15_000);
});

test("a temporary disconnection resumes and completes, calling onTerminal once", async () => {
  const fetch = vi.fn<() => Promise<Op>>()
    .mockRejectedValueOnce(new TypeError("offline")).mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValue({state: "succeeded"});
  const onTerminal = vi.fn();
  render(<Probe fetch={fetch} onTerminal={onTerminal}/>);
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
  fetch.mockResolvedValue({state: "succeeded"});
  render(<Probe fetch={fetch}/>);
  act(() => screen.getByText("start").click());
  await go(10_000);
  const calls = fetch.mock.calls.length;
  await act(async () => { window.dispatchEvent(new Event("online")); await vi.advanceTimersByTimeAsync(0); });
  expect(fetch.mock.calls.length).toBe(calls + 1);
});

test("a fatal error stops observation with its message", async () => {
  render(<Probe fetch={async () => { throw new FatalObservationError("bad shape"); }}/>);
  act(() => screen.getByText("start").click());
  await go(1_000);
  expect(screen.getByTestId("fatal")).toHaveTextContent("bad shape");
});
