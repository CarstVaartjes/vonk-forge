import {readFileSync} from "node:fs";
import {webcrypto} from "node:crypto";
import {Blob as NativeBlob} from "node:buffer";
import {contractEqual} from "./contract-numeric";
import {validateComponent} from "./contract-json";
import {ObservationUnavailable, readObservationTransfer} from "./observation-transfer";
import {stringifyContractJson} from "./contract-numeric";

beforeEach(() => { vi.stubGlobal("crypto", webcrypto); vi.stubGlobal("Blob", NativeBlob); });
afterEach(() => vi.unstubAllGlobals());

const transfer_id = "00000000-0000-4000-8000-000000000001";
const payload = {event_cursor: 0, generated_at: "2026-10-07T00:00:00Z", authority_revision: "a".repeat(64), nodes: []};

async function records() {
  const bytes = new TextEncoder().encode(stringifyContractJson(payload));
  const sha = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  const data = btoa(String.fromCharCode(...bytes));
  return [
    {type: "start", transfer_id, resource: "fleet", encoding: "base64-canonical-json-utf8-v1"},
    {type: "chunk", transfer_id, ordinal: 0, data},
    {type: "complete", transfer_id, chunks: 1, bytes: bytes.byteLength, sha256: Array.from(sha, b => b.toString(16).padStart(2, "0")).join("")},
  ];
}
function response(values: unknown[]) {
  // Split across arbitrary UTF-8 stream boundaries, including a record newline.
  const bytes = new TextEncoder().encode(values.map(value => stringifyContractJson(value) + "\n").join(""));
  return new Response(new ReadableStream({start(controller) {
    controller.enqueue(bytes.slice(0, 17)); controller.enqueue(bytes.slice(17)); controller.close();
  }}), {headers: {"Content-Type": "application/x-vonk-observation+ndjson"}});
}

test("publishes only a complete canonically validated observation", async () => {
  expect(await readObservationTransfer(response(await records()), "/api/fleet")).toEqual(payload);
});
test("does not publish disconnected, corrupted or extra-record observations, then recovers", async () => {
  const values = await records();
  await expect(readObservationTransfer(response(values.slice(0, 2)), "/api/fleet")).rejects.toThrow();
  await expect(readObservationTransfer(response([...values, values[1]]), "/api/fleet")).rejects.toThrow();
  await expect(readObservationTransfer(response([values[0], {...values[1], ordinal: 1}, values[2]]), "/api/fleet")).rejects.toThrow();
  await expect(readObservationTransfer(response([values[0], values[1], {...values[2], sha256: "0".repeat(64)}]), "/api/fleet")).rejects.toThrow();
  await expect(readObservationTransfer(response([values[0], {...values[1], transfer_id: "00000000-0000-4000-8000-000000000002"}, values[2]]), "/api/fleet")).rejects.toThrow();
  expect(await readObservationTransfer(response(values), "/api/fleet")).toEqual(payload);
});

interface ProducerFixture {name: string; path: string; media_type: string; body: string; payload_component: string; payload_json: string}
const fixturePath = process.env.VONK_OBSERVATION_TRANSFERS;
const producerFixtures: ProducerFixture[] = fixturePath ? JSON.parse(readFileSync(fixturePath, "utf8")) : [];
test.runIf(Boolean(fixturePath))("actual Python producer transfers preserve wide cursors and indivisible membership", async () => {
  expect(producerFixtures.map(item => item.name)).toEqual(["large-indivisible-fleet", "small-fleet"]);
  for (const item of producerFixtures) {
    if (item.name === "large-indivisible-fleet") expect(new TextEncoder().encode(item.payload_json).byteLength).toBeGreaterThan(1048576);
    const body = new TextEncoder().encode(item.body);
    const transport = new ReadableStream({start(controller) {
      // Transport chunk boundaries deliberately do not match record boundaries.
      for (let offset = 0; offset < body.length; offset += 65521) controller.enqueue(body.slice(offset, offset + 65521));
      controller.close();
    }});
    const observed = await readObservationTransfer(new Response(transport, {headers: {"Content-Type": item.media_type}}), item.path);
    expect(contractEqual(observed, validateComponent(item.payload_component, item.payload_json))).toBe(true);
  }
});

test("retains the canonical terminal owner cause", async () => {
  const values = await records();
  await expect(readObservationTransfer(response([values[0], {type: "error", transfer_id,
    reason_code: "observation.transfer_unavailable", detail: "Capture serialization unavailable"}]), "/api/fleet"))
    .rejects.toEqual(new ObservationUnavailable("observation.transfer_unavailable", "Capture serialization unavailable"));
});
test("rejects a record UTF-8 BOM instead of removing it", async () => {
  const values = await records();
  const body = new Uint8Array([239, 187, 191, ...new TextEncoder().encode(values.map(value => stringifyContractJson(value) + "\n").join(""))]);
  await expect(readObservationTransfer(new Response(body, {headers: {"Content-Type": "application/x-vonk-observation+ndjson"}}), "/api/fleet")).rejects.toThrow();
});
