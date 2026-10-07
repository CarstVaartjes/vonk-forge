import {readFileSync} from "node:fs";
import {afterEach, expect, test, vi} from "vitest";
import {ApiClient, ApiError} from "./client";
import {ContractViolation, validateComponent} from "./contract-json";
import {contractEqual, parseContractJson, stringifyContractJson} from "./contract-numeric";

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
afterEach(() => vi.unstubAllGlobals());

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
  expect(contractEqual(value, parseContractJson(item.normalized_text ?? item.text))).toBe(true);
  // The retained DTO can still be exported as JSON numeric values, not strings.
  expect(contractEqual(parseContractJson(stringifyContractJson(value)), parseContractJson(item.normalized_text ?? item.text))).toBe(true);
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
    else expect(contractEqual(await read(), parseContractJson(item.normalized_text ?? item.text))).toBe(true);
  }
});
