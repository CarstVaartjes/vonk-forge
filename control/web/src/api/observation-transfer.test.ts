import {readObservationTransfer} from "./observation-transfer";
import {stringifyContractJson} from "./contract-numeric";

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
