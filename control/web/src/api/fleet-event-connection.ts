import {contractRoutes} from "./runtime.generated.js";
import type {components} from "./generated";
import {formatWire, type WireNumber} from "./contract-numeric";
import {ContractViolation, validateComponent} from "./contract-json";

export function matchesFleetCursor(identifier: string, canonicalCursor: WireNumber): boolean {
  return /^[0-9]+$/.test(identifier) && identifier.replace(/^0+(?=[0-9])/, "") === formatWire(canonicalCursor);
}

function retryAfterMilliseconds(value: string | null): bigint | undefined {
  if (value === null) return undefined;
  if (/^[0-9]+$/.test(value)) return BigInt(value) * 1000n;
  // Retry-After also permits an HTTP-date. Date milliseconds are native
  // transport scheduling values, never canonical Controller counters.
  const deadline = Date.parse(value);
  return Number.isFinite(deadline) ? BigInt(Math.max(0, deadline - Date.now())) : undefined;
}

/** WHATWG SSE fields and delimiters, with the canonical route's byte owner.
 * https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream
 * Invalid UTF-8 is refused by the Control contract rather than repaired.
 */
export async function readFleetEvents(response: Response, dispatch: (event: MessageEvent<string>) => void, retry: (milliseconds: bigint) => void): Promise<void> {
  const path = "/api/fleet/stream";
  const limit = contractRoutes.find(route => route.route === path && route.method === "GET")?.frameMaxBytes;
  if (!limit || !response.body) throw new ContractViolation("GET", path, response.status);
  const byteLimit = limit;
  const reader = response.body.getReader();
  const line = new Uint8Array(limit);
  const decoder = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true});
  let lineBytes = 0, frameBytes = 0;
  let carriageReturn = false, firstLine = true;
  let identifier = "", eventType = "";
  let data: string[] = [];
  function processLine(): void {
    let text = decoder.decode(line.subarray(0, lineBytes));
    lineBytes = 0;
    if (firstLine) { firstLine = false; if (text.startsWith("\uFEFF")) text = text.slice(1); }
    if (!text) {
      if (data.length) dispatch(new MessageEvent(eventType || "message", {data: data.join("\n"), lastEventId: identifier, origin: new URL(response.url || location.origin).origin}));
      data = []; eventType = ""; frameBytes = 0;
      return;
    }
    if (text.startsWith(":")) return;
    const colon = text.indexOf(":");
    const field = colon < 0 ? text : text.slice(0, colon);
    let value = colon < 0 ? "" : text.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") data.push(value);
    else if (field === "event") eventType = value;
    else if (field === "id" && !value.includes("\0")) identifier = value;
    else if (field === "retry" && /^[0-9]+$/.test(value)) retry(BigInt(value));
  }
  function count(): void {
    frameBytes += 1;
    if (frameBytes > byteLimit) throw new ContractViolation("GET", path, response.status);
  }
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      for (const byte of value) {
        if (carriageReturn) {
          carriageReturn = false;
          if (byte === 10) { count(); processLine(); continue; }
          processLine();
        }
        // Account actual wire bytes before retaining or decoding a line. The
        // browser owns the upstream fetch chunk, not this application buffer.
        count();
        if (byte === 13) carriageReturn = true;
        else if (byte === 10) processLine();
        else line[lineBytes++] = byte;
      }
    }
    if (carriageReturn) processLine();
    // EOF without a blank line discards pending data, per SSE semantics.
  } catch (cause) {
    try { await reader.cancel(); } catch { /* Keep the original failure. */ }
    throw cause;
  } finally { reader.releaseLock(); }
}

export interface FleetEventStream {
  readonly url: string;
  close(): void;
  addEventListener(type: string, listener: EventListener): void;
  removeEventListener(type: string, listener: EventListener): void;
}

/** One connection at a time; only applied domain state supplies reconnect ID. */
export class FleetEventConnection extends EventTarget {
  readonly url = "/api/fleet/stream";
  private controller?: AbortController;
  private timer?: ReturnType<typeof setTimeout>;
  private retryMilliseconds = 2000n;
  private closed = false;
  constructor(private readonly appliedCursor: () => string, private readonly authorize: (response: Response) => void = () => undefined) {
    super();
    void this.connect();
  }
  close(): void {
    this.closed = true;
    if (this.timer !== undefined) clearTimeout(this.timer);
    this.timer = undefined;
    this.controller?.abort();
  }
  private reconnect(remaining = this.retryMilliseconds): void {
    if (this.closed) return;
    // Browser timers use signed 32-bit milliseconds. Preserve a larger server
    // delay in consecutive cancellable waits, never clamp it to an early retry.
    const part = remaining > 2147483647n ? 2147483647n : remaining;
    this.timer = setTimeout(() => {
      this.timer = undefined;
      if (this.closed) return;
      if (remaining > part) this.reconnect(remaining - part);
      else void this.connect();
    }, Number(part));
  }
  private async connect(): Promise<void> {
    if (this.closed) return;
    const controller = new AbortController();
    this.controller = controller;
    let readingCanonicalEvents = false;
    try {
      const headers = new Headers({Accept: "text/event-stream"});
      const cursor = this.appliedCursor();
      if (cursor) headers.set("Last-Event-ID", cursor);
      const response = await fetch(this.url, {headers, credentials: "same-origin", cache: "no-store", signal: controller.signal});
      if (this.closed || controller.signal.aborted) { await response.body?.cancel(); return; }
      if (response.status === 401) {
        await response.body?.cancel();
        this.closed = true;
      }
      this.authorize(response);
      if (response.status === 204) { this.close(); this.dispatchEvent(new Event("error")); return; }
      if (response.status !== 200 || response.headers.get("Content-Type")?.split(";")[0].trim() !== "text/event-stream") {
        const retryAfter = retryAfterMilliseconds(response.headers.get("Retry-After"));
        if (retryAfter !== undefined && retryAfter > this.retryMilliseconds) this.retryMilliseconds = retryAfter;
        await response.body?.cancel();
        // Proxy/server outages and a temporarily wrong media response can
        // recover on the same owner. Explicit refusal/unsupported 4xx cannot.
        if (response.status >= 400 && response.status < 500) this.closed = true;
        throw new ContractViolation("GET", this.url, response.status);
      }
      readingCanonicalEvents = true;
      this.dispatchEvent(new Event("open"));
      await readFleetEvents(response, event => {
        const owner = contractRoutes.find(route => route.method === "GET" && route.route === this.url)?.sseEvents?.[event.type];
        if (!owner) throw new ContractViolation("GET", this.url, response.status);
        const payload = validateComponent(owner, event.data) as components["schemas"]["FleetStreamEvent"];
        if (!matchesFleetCursor(event.lastEventId, payload.event_cursor)
            || ("sample" in payload && payload.sample.node_id !== payload.node_id)) {
          throw new ContractViolation("GET", this.url, response.status);
        }
        if (!this.closed) this.dispatchEvent(event);
      }, milliseconds => { this.retryMilliseconds = milliseconds; });
    } catch (cause) {
      if (controller.signal.aborted) return;
      if (readingCanonicalEvents && cause instanceof ContractViolation) this.dispatchEvent(new Event("unavailable"));
    } finally {
      this.controller = undefined;
    }
    if (!controller.signal.aborted) this.dispatchEvent(new Event("error"));
    if (!this.closed) this.reconnect();
  }
}
