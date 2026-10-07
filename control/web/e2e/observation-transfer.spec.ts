import {readFileSync} from "node:fs";
import {expect, test} from "@playwright/test";

interface Fixture {name: string; path: string; media_type: string; body: string; payload_component: string; payload_json: string}
const fixtureFile = process.env.VONK_OBSERVATION_TRANSFERS;
const fixtures: Fixture[] = fixtureFile ? JSON.parse(readFileSync(fixtureFile, "utf8")) : [];

test("Chromium installs real complete Python observations with indivisible membership and exact wide cursor", async ({page}) => {
  expect(fixtures.map(item => item.name)).toEqual(["large-indivisible-fleet", "small-fleet"]);
  await page.goto("/");
  for (const item of fixtures) {
    await page.route(url => url.pathname === item.path, route => route.fulfill({status: 200, contentType: item.media_type, body: item.body}));
    const observed = await page.evaluate(async ({expectedText, component}) => {
      const clientPath = "/src/api/client.ts", numericPath = "/src/api/contract-numeric.ts", contractPath = "/src/api/contract-json.ts";
      const {ApiClient} = await import(clientPath);
      const {contractEqual, stringifyContractJson} = await import(numericPath);
      const {validateComponent} = await import(contractPath);
      const value = await new ApiClient().visualFleet();
      return {equal: contractEqual(value, validateComponent(component, expectedText)), text: stringifyContractJson(value)};
    }, {expectedText: item.payload_json, component: item.payload_component});
    expect(observed.equal).toBe(true);
    expect(observed.text).toContain('"event_cursor":9223372036854775807');
    if (item.name === "large-indivisible-fleet") expect(new TextEncoder().encode(item.payload_json).byteLength).toBeGreaterThan(1048576);
    await page.unroute(url => url.pathname === item.path);
  }
});

test("Chromium rejects an oversized SSE peer frame and later reads a bounded canonical refresh", async ({page}) => {
  const path = "/api/fleet/stream";
  await page.route(url => url.pathname === path, route => route.fulfill({status: 200, contentType: "text/event-stream", body: "x".repeat(1048577)}));
  await page.goto("/");
  const refused = await page.evaluate(async () => {
    const modulePath = "/src/api/fleet-event-connection.ts";
    const {readFleetEvents} = await import(modulePath);
    let dispatched = 0;
    try {
      await readFleetEvents(await fetch("/api/fleet/stream"), () => { dispatched += 1; }, () => undefined);
      return {refused: false, dispatched};
    } catch { return {refused: true, dispatched}; }
  });
  expect(refused).toEqual({refused: true, dispatched: 0});
  await page.unroute(url => url.pathname === path);
  const recoveryFile = fixtureFile?.replace(/observation-transfers\.json$/, "sparse-event-recovery.json");
  expect(recoveryFile).toBeDefined();
  const recovery: {notice: string; repaired_frame: string; event_cursor: number; path: string; media_type: string; body: string; payload_json: string} = JSON.parse(readFileSync(recoveryFile!, "utf8"));
  await page.route(url => url.pathname === path, route => route.fulfill({status: 200, contentType: "text/event-stream", body: recovery.notice}));
  await page.route(url => url.pathname === recovery.path, route => route.fulfill({status: 200, contentType: recovery.media_type, body: recovery.body}));
  const recovered = await page.evaluate(async ({expected}) => {
    const modulePath = "/src/api/fleet-event-connection.ts", contractPath = "/src/api/contract-json.ts", clientPath = "/src/api/client.ts", numericPath = "/src/api/contract-numeric.ts";
    const {readFleetEvents} = await import(modulePath), {validateComponent} = await import(contractPath);
    const {ApiClient} = await import(clientPath), {contractEqual} = await import(numericPath);
    const values: unknown[] = [];
    await readFleetEvents(await fetch("/api/fleet/stream"), event => values.push(validateComponent("FleetRefreshEvent", event.data)), () => undefined);
    // The notice carries no roster. Only the completed transfer may publish it.
    const snapshot = await new ApiClient().visualFleet();
    return {values, equal: contractEqual(snapshot, validateComponent("FleetSnapshot", expected)), cursor: snapshot.event_cursor};
  }, {expected: recovery.payload_json});
  expect(recovered.values).toHaveLength(1);
  expect(recovered.values[0]).toMatchObject({event_cursor: recovery.event_cursor, reset_reason: "frame-unavailable", issue: {reason_code: "fleet.frame_budget_exceeded"}});
  expect(recovered.equal).toBe(true);
  expect(recovered.cursor).toBe(recovery.event_cursor);
  await page.unroute(url => url.pathname === path);
  await page.route(url => url.pathname === path, route => route.fulfill({status: 200, contentType: "text/event-stream", body: recovery.repaired_frame}));
  const repaired = await page.evaluate(async () => {
    const modulePath = "/src/api/fleet-event-connection.ts", contractPath = "/src/api/contract-json.ts";
    const {readFleetEvents} = await import(modulePath), {validateComponent} = await import(contractPath);
    const events: {type: string; id: string; data: unknown}[] = [];
    await readFleetEvents(await fetch("/api/fleet/stream"), event => events.push({type: event.type, id: event.lastEventId, data: validateComponent("FleetChangeEvent", event.data)}), () => undefined);
    return events;
  });
  expect(repaired).toHaveLength(1);
  expect(repaired[0]).toMatchObject({type: "operation-state", id: String(recovery.event_cursor), data: {change: {fields: {kind: "deploy"}}}});
});
