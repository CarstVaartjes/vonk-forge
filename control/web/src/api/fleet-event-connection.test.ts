import {FleetEventConnection, readFleetEvents} from "./fleet-event-connection";
import {ContractViolation, validateComponent} from "./contract-json";
import {stringifyContractJson} from "./contract-numeric";

function response(body: string) {
  const bytes = new TextEncoder().encode(body);
  return new Response(new ReadableStream({start(controller) {
    // Byte-at-a-time delivery splits CRLF, BOM and multibyte UTF-8 characters.
    for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
    controller.close();
  }}), {headers: {"Content-Type": "text/event-stream"}});
}
const notice = {reset_reason: "initial", event_cursor: 5};
const frame = `id: 5\nevent: fleet-refresh\ndata: ${stringifyContractJson(notice)}\n\n`;
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

test("reads SSE BOM, CRLF/CR/LF, multiline data, retry, persistent IDs and empty ID reset", async () => {
  const events: MessageEvent<string>[] = [], retries: bigint[] = [];
  await readFleetEvents(response('\uFEFF: comment\r\nretry: 2500\r\nevent: custom\r\nid: 5\r\ndata: first é\r\ndata: second\r\n\r\ndata: inherited\r\r id: ignored\n\nid\ndata: reset\n\ndata: unfinished'),
    event => events.push(event), value => retries.push(value));
  expect(events.map(event => [event.type, event.data, event.lastEventId])).toEqual([
    ["custom", "first é\nsecond", "5"], ["message", "inherited", "5"], ["message", "reset", ""],
  ]);
  expect(retries).toEqual([2500n]);
});

test("rejects a malformed peer's oversized unterminated frame before dispatch and cancels it", async () => {
  let cancelled = false;
  const dispatch = vi.fn();
  const malicious = new Response(new ReadableStream({start(controller) {
    controller.enqueue(new Uint8Array(1048577).fill(120));
  }, cancel() { cancelled = true; }}), {headers: {"Content-Type": "text/event-stream"}});
  await expect(readFleetEvents(malicious, dispatch, () => undefined)).rejects.toBeInstanceOf(ContractViolation);
  expect(dispatch).not.toHaveBeenCalled();
  expect(cancelled).toBe(true);
});

test("schema-validates actual streamed refresh data before it can request snapshot adoption", async () => {
  const received: unknown[] = [];
  await readFleetEvents(response(frame), event => received.push(validateComponent("FleetRefreshEvent", event.data)), () => undefined);
  expect(received).toEqual([notice]);
});

async function flush() { for (let index = 0; index < 8; index += 1) await Promise.resolve(); }

test("reconnects one request at a time using only the applied cursor and server retry delay", async () => {
  vi.useFakeTimers();
  const requests: RequestInit[] = [];
  let secondSignal: AbortSignal | null | undefined;
  const fetcher = vi.fn(async (_path: string, init: RequestInit) => {
    requests.push(init);
    if (requests.length === 1) return response("retry: 2500\n" + frame);
    secondSignal = init.signal;
    return new Response(new ReadableStream({start() { /* live connection */ }}), {headers: {"Content-Type": "text/event-stream"}});
  });
  vi.stubGlobal("fetch", fetcher);
  let applied = "5";
  const connection = new FleetEventConnection(() => applied);
  const received = vi.fn();
  connection.addEventListener("fleet-refresh", received);
  await flush();
  expect(received).toHaveBeenCalledTimes(1);
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(requests[0].credentials).toBe("same-origin");
  expect(requests[0].cache).toBe("no-store");
  applied = "6";
  vi.advanceTimersByTime(2499);
  await flush();
  expect(fetcher).toHaveBeenCalledTimes(1);
  vi.advanceTimersByTime(1);
  await flush();
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(new Headers(requests[1].headers).get("Last-Event-ID")).toBe("6");
  connection.close();
  expect(secondSignal?.aborted).toBe(true);
  vi.advanceTimersByTime(10000);
  expect(fetcher).toHaveBeenCalledTimes(2);
});

test("204 closes permanently and 401 invokes the existing authentication owner without reconnecting", async () => {
  vi.useFakeTimers();
  for (const status of [204, 401]) {
    const authorize = vi.fn((reply: Response) => { if (reply.status === 401) throw new Error("Authentication required"); });
    const fetcher = vi.fn().mockResolvedValue(new Response(null, {status}));
    vi.stubGlobal("fetch", fetcher);
    const connection = new FleetEventConnection(() => "", authorize);
    const error = vi.fn(); connection.addEventListener("error", error);
    await flush();
    expect(error).toHaveBeenCalledTimes(1);
    expect(authorize).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(10000); await flush();
    expect(fetcher).toHaveBeenCalledTimes(1);
    connection.close();
  }
});

test("canonical frame issues refuse contradictory cause evidence", () => {
  const document = (reason_code: string, observed_bytes_at_least: number | null, budget_bytes = 1048576) => stringifyContractJson({
    reset_reason: "frame-unavailable", event_cursor: 7, issue: {reason_code, observed_bytes_at_least, budget_bytes},
  });
  expect(() => validateComponent("FleetRefreshEvent", document("fleet.frame_encoding_unavailable", 1048577))).toThrow();
  expect(() => validateComponent("FleetRefreshEvent", document("fleet.frame_budget_exceeded", null))).toThrow();
  expect(() => validateComponent("FleetRefreshEvent", document("fleet.frame_budget_exceeded", 1048576))).toThrow();
  expect(() => validateComponent("FleetRefreshEvent", document("fleet.frame_budget_exceeded", 1048577, 2))).toThrow();
  expect(() => validateComponent("FleetRefreshEvent", document("fleet.frame_encoding_unavailable", null))).not.toThrow();
});


test("rejects canonical payload/header mismatch before dispatch and reconnects from applied authority", async () => {
  vi.useFakeTimers();
  const requests: RequestInit[] = [];
  const fetcher = vi.fn(async (_path: string, init: RequestInit) => {
    requests.push(init);
    if (requests.length === 1) return response(frame.replace("id: 5", "id: 999999999999999999999999"));
    return new Response(new ReadableStream({start() { /* live */ }}), {headers: {"Content-Type": "text/event-stream"}});
  });
  vi.stubGlobal("fetch", fetcher);
  const connection = new FleetEventConnection(() => "5");
  const received = vi.fn(), unavailable = vi.fn();
  connection.addEventListener("fleet-refresh", received);
  connection.addEventListener("unavailable", unavailable);
  await flush();
  expect(received).not.toHaveBeenCalled();
  expect(unavailable).toHaveBeenCalledTimes(1);
  vi.advanceTimersByTime(3000); await flush();
  expect(new Headers(requests[1].headers).get("Last-Event-ID")).toBe("5");
  connection.close();
});
