import {contractRoutes} from "./runtime.generated.js";
import type {components} from "./generated";
import {ContractViolation, validateComponent, validateControlBody} from "./contract-json";
import {formatWire} from "./contract-numeric";

type Record = components["schemas"]["ObservationTransferRecord"];
const MEDIA = "application/x-vonk-observation+ndjson";

/** A record allocation does not cap the completed model or the browser heap. */
export async function readObservationTransfer(response: Response, path: string): Promise<unknown> {
  const owner = contractRoutes.find(route => route.method === "GET" && route.route === path);
  const limit = owner?.recordMaxBytes;
  const payload = owner?.observationPayload;
  const invalid = () => new ContractViolation("GET", path, response.status);
  if (response.status !== 200 || response.headers.get("content-type")?.split(";")[0].trim() !== MEDIA
      || !limit || !payload || !response.body) throw invalid();
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", {fatal: true});
  let pending: Uint8Array[] = [];
  let pendingBytes = 0;
  let transferId: string | undefined;
  let ordinal = 0n;
  let bytes = 0n;
  let complete: Extract<Record, {type: "complete"}> | undefined;
  const fragments: ArrayBuffer[] = [];
  function record(parts: Uint8Array[], size: number): void {
    if (complete) throw invalid();
    const line = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) { line.set(part, offset); offset += part.byteLength; }
    // The only type boundary follows the canonical generated union validator.
    const value = validateControlBody("GET", path, 200, MEDIA, decoder.decode(line)) as Record;
    if (!transferId) {
      if (value.type !== "start" || value.resource !== (path === "/api/fleet" ? "fleet" : "platform")) throw invalid();
      transferId = value.transfer_id;
      return;
    }
    if (value.transfer_id !== transferId) throw invalid();
    if (value.type === "chunk") {
      if (BigInt(formatWire(value.ordinal)) !== ordinal) throw invalid();
      const binary = atob(value.data);
      // The schema owns canonical base64; this check also fences host decoders.
      if (btoa(binary) !== value.data || binary.length === 0) throw invalid();
      const fragment = Uint8Array.from(binary, character => character.charCodeAt(0));
      fragments.push(fragment.buffer);
      ordinal += 1n;
      bytes += BigInt(fragment.byteLength);
    } else if (value.type === "complete") {
      if (BigInt(formatWire(value.chunks)) !== ordinal || BigInt(formatWire(value.bytes)) !== bytes) throw invalid();
      complete = value;
    } else throw invalid();
  }
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      let start = 0;
      for (let index = 0; index < value.byteLength; index += 1) {
        if (value[index] !== 10) continue;
        const part = value.subarray(start, index);
        if (pendingBytes + part.byteLength + 1 > limit) throw invalid();
        pending.push(part);
        record(pending, pendingBytes + part.byteLength);
        pending = []; pendingBytes = 0;
        start = index + 1;
      }
      const tail = value.subarray(start);
      if (pendingBytes + tail.byteLength + 1 > limit) throw invalid();
      if (tail.byteLength) { pending.push(tail.slice()); pendingBytes += tail.byteLength; }
    }
    if (pendingBytes || !complete) throw invalid();
    // The final model inherently requires aggregate memory. No snapshot size is
    // guessed from a per-record transport allocation, and no facts are clipped.
    const blob = new Blob(fragments);
    const buffer = await blob.arrayBuffer();
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", buffer));
    const hex = Array.from(digest, byte => byte.toString(16).padStart(2, "0")).join("");
    if (hex !== complete.sha256) throw invalid();
    return validateComponent(payload, decoder.decode(buffer));
  } catch (cause) {
    try { await reader.cancel(); } catch { /* Preserve the owning failure. */ }
    throw cause;
  } finally { reader.releaseLock(); }
}
