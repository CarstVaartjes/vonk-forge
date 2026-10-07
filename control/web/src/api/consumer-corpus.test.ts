import {readFileSync, writeFileSync} from "node:fs";
import {afterAll, afterEach, expect, test, vi} from "vitest";
import {ApiClient, ApiError} from "./client";
import {ContractViolation, validateComponent} from "./contract-json";
import {contractEqual, stringifyContractJson} from "./contract-numeric";

interface ConsumerCase {
  id: string;
  component: string;
  text: string;
  accepted: boolean;
  normalized_text: string | null;
  consumers: string[];
  http?: {method: string; path: string; status: number; text: string};
  browserGeneratedMethod?: "job";
}
const file = process.env.VONK_CONSUMER_CORPUS;
// This is a source-bound hosted producer artifact, not another schema owner.
const corpus: {version: number; cases: ConsumerCase[]} = file
  ? JSON.parse(readFileSync(file, "utf8"))
  : {version: 1, cases: []};
const cases = corpus.cases.filter(item => item.consumers.includes("browser"));
const exportedCases: {id: string; text: string}[] = [];
afterEach(() => vi.unstubAllGlobals());
afterAll(() => {
  const output = process.env.VONK_CONSUMER_OUTPUT;
  if (output) writeFileSync(output, JSON.stringify({version: 1, exports: exportedCases}) + "\n");
});

test.runIf(Boolean(file))("hosted corpus contains real consumer cases", () => {
  expect(corpus.version).toBe(1);
  expect(cases.length).toBeGreaterThan(0);
  expect(cases.some(item => item.http && item.browserGeneratedMethod === "job")).toBe(true);
});
test.each(cases)("canonical browser component: $id", item => {
  if (!item.accepted) {
    expect(() => validateComponent(item.component, item.text)).toThrow();
    return;
  }
  const value = validateComponent(item.component, item.text);
  const text = stringifyContractJson(value);
  expect(contractEqual(value, validateComponent(item.component, text))).toBe(true);
  // The owning Python model checks defaults, timestamps, exact scalar kinds and
  // IEEE zero signs on both documents in the connected hosted verifier.
  exportedCases.push({id: item.id, text});
});
test.each(cases.filter(item => item.http && item.browserGeneratedMethod === "job"))("actual raw and generated HTTP consumers: $id", async item => {
  const http = item.http!;
  vi.stubGlobal("fetch", async () => new Response(http.text, {status: http.status, headers: {"content-type": "application/json", "x-request-id": "corpus-request"}}));
  const client = new ApiClient();
  const raw = () => client.request(http.path, {method: http.method});
  const generated = () => client.job(decodeURIComponent(http.path.split("/").at(-1)!));
  for (const read of [raw, generated]) {
    if (!item.accepted) await expect(read()).rejects.toBeInstanceOf(ContractViolation);
    else if (http.status >= 400) await expect(read()).rejects.toBeInstanceOf(ApiError);
    else expect(contractEqual(await read(), validateComponent(item.component, http.text))).toBe(true);
  }
});
