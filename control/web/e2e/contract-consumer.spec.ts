import {readFileSync, writeFileSync} from "node:fs";
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
  await page.route(url => url.pathname === http.path, route => route.fulfill({status: http.status, contentType: "application/json", body: http.text}));
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


test("Chromium recipe setting editor sends the exact wide integer through the real HTTP client", async ({page, context}) => {
  expect(corpusFile).toBeDefined();
  const fixtureFile = corpusFile!.replace(/[^/]+$/, "artifact-workspace.json");
  const source = readFileSync(fixtureFile, "utf8");
  const routing = JSON.parse(source);
  const requests: string[] = [];
  await context.addCookies([{name: "vonk_csrf", value: "fixture-csrf", url: "http://127.0.0.1:4174"}]);
  await page.route(url => url.pathname === "/api/artifact-jobs/capabilities", route => route.fulfill({status: 200, contentType: "application/json", body: routing.capabilities_json}));
  await page.route(url => url.pathname === "/api/fleet", route => route.fulfill({status: 200, contentType: routing.fleet_media_type, body: routing.fleet_body}));
  await page.route(url => url.pathname === `/api/recipe/runs/${routing.run_id}/artifact-jobs`, async route => {
    if (route.request().method() === "GET") {
      await route.fulfill({status: 200, contentType: "application/json", body: routing.jobs_json});
    } else {
      requests.push(route.request().postData()!);
      expect(route.request().headers()["x-csrf-token"]).toBe("fixture-csrf");
      expect(route.request().headers()["x-request-id"]).toMatch(/^[0-9a-f-]{36}$/);
      await route.fulfill({status: routing.refusal.status, contentType: routing.refusal.media_type, body: routing.refusal.body});
    }
  });
  await page.goto("/");
  await page.evaluate(async source => {
    const path = "/e2e/artifact-workspace-mount.tsx";
    const {mountArtifactWorkspace} = await import(path);
    const fixture = JSON.parse(source);
    await mountArtifactWorkspace(fixture.definition_json, fixture.revision_id, fixture.content_sha256);
  }, source);
  const seed = page.getByRole("textbox", {name: "Seed", exact: true});
  await expect(seed).toHaveValue("9007199254740993");
  await seed.fill("9007199254740995");
  await expect(seed).toHaveValue("9007199254740995");
  await page.getByRole("button", {name: "Submit artifact job"}).click();
  await expect.poll(() => requests.length).toBeGreaterThan(0);
  expect(requests[0]).toContain('"seed":9007199254740995');
  expect(requests[0]).not.toContain('"seed":9007199254740996');
  writeFileSync(corpusFile!.replace(/[^/]+$/, "artifact-workspace-request.json"), requests[0]);
});
