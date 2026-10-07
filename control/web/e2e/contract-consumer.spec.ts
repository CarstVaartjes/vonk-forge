import {readFileSync} from "node:fs";
import {expect, test} from "@playwright/test";

interface HttpCase {id: string; accepted: boolean; consumers: string[]; browserGeneratedMethod?: string; http?: {path: string; status: number; text: string}}
const corpusFile = process.env.VONK_CONSUMER_CORPUS;
const cases: HttpCase[] = corpusFile ? JSON.parse(readFileSync(corpusFile, "utf8")).cases : [];

test("Chromium retains original numeric tokens and own prototype keys", async ({page}) => {
  await page.goto("/");
  const exported = await page.evaluate(async () => {
    const path = "/src/api/contract-numeric.ts";
    const {parseContractJson, stringifyContractJson} = await import(path);
    return stringifyContractJson(parseContractJson('{"__proto__":{"own":true},"value":9007199254740993,"float":1000.0}'));
  });
  expect(exported).toBe('{"__proto__":{"own":true},"value":9007199254740993,"float":1000.0}');
});

test("Chromium exercises both actual HTTP consumers with the same canonical body", async ({page}) => {
  const item = cases.find(item => item.id === "job-diagnostics-numeric-envelope" && item.accepted && item.consumers.includes("browser") && item.browserGeneratedMethod === "job" && item.http?.status === 200);
  expect(item, "Hosted canonical producer must supply a real Job response").toBeDefined();
  const http = item!.http!;
  await page.route(`**${http.path}`, route => route.fulfill({status: http.status, contentType: "application/json", body: http.text}));
  await page.goto("/");
  const exported = await page.evaluate(async ({path}) => {
    const clientPath = "/src/api/client.ts", numericPath = "/src/api/contract-numeric.ts";
    const {ApiClient} = await import(clientPath), {stringifyContractJson} = await import(numericPath);
    const client = new ApiClient();
    return [stringifyContractJson(await client.request(path)), stringifyContractJson(await client.job(decodeURIComponent(path.split("/").at(-1)!)))];
  }, {path: http.path});
  expect(exported[0]).toBe(exported[1]);
  // The native source-token capability must preserve real shared diagnostics.
  expect(exported[0]).toContain("9007199254740993");
});
