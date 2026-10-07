import type {Page} from "@playwright/test";
import {createHash} from "node:crypto";
import type {components} from "../src/api/generated";
import {stringifyContractJson} from "../src/api/contract-numeric";

/** A complete typed transfer, consumed by the real browser NDJSON reader. */
export async function serveEmptyFleet(page: Page) {
  const snapshot: components["schemas"]["FleetSnapshot"] = {
    event_cursor: 0, generated_at: new Date().toISOString(), authority_revision: "a".repeat(64), nodes: [],
  };
  const bytes = Buffer.from(stringifyContractJson(snapshot), "utf8");
  const transfer_id = "00000000-0000-4000-8000-000000000001";
  const records: components["schemas"]["ObservationTransferRecord"][] = [
    {type: "start", transfer_id, resource: "fleet", encoding: "base64-canonical-json-utf8-v1"},
    {type: "chunk", transfer_id, ordinal: 0, data: bytes.toString("base64")},
    {type: "complete", transfer_id, chunks: 1, bytes: bytes.byteLength, sha256: createHash("sha256").update(bytes).digest("hex")},
  ];
  await page.route("**/api/fleet", route => route.fulfill({
    contentType: "application/x-vonk-observation+ndjson",
    body: records.map(record => stringifyContractJson(record) + "\n").join(""),
  }));
  // These layout journeys have no subsequent Fleet changes to observe.
  await page.route("**/api/fleet/stream", route => route.fulfill({status: 204}));
}
