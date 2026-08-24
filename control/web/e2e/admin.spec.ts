import {expect, test} from "@playwright/test";

const commit = "a".repeat(40);

test.beforeEach(async ({page}) => {
  await page.route("**/api/v1/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
});

test("the redesigned shell exposes the focused workspace routes", async ({page}) => {
  await page.route("**/api/v1/fleet", route => route.fulfill({json: {schema_version: 1, event_cursor: 0, generated_at: new Date().toISOString(), authority_revision: commit, nodes: []}}));
  await page.route("**/api/v1/library**", route => route.fulfill({json: {
    schema_version: 1,
    generated_at: new Date().toISOString(),
    freshness_policy: {inventory_fresh_seconds: 300, telemetry_live_seconds: 6, telemetry_delayed_seconds: 20},
    models: [],
    unlinked_recipes: [],
    next_cursor: null,
  }}));
  await page.goto("/library");
  await expect(page.getByRole("heading", {name: "Library", exact: true})).toBeVisible();
  const primaryLinks = page.getByRole("navigation", {name: "Primary"}).getByRole("link");
  await expect(primaryLinks).toHaveText(["Fleet", "Library", "Activity"]);
  await expect(page).toHaveURL(/\/library$/);
});

test("Activity combines friendly audit history and current operations", async ({page}) => {
  const requestId = "f6e73ce3-3329-4ff4-b086-d8f87c879ce9";
  await page.route("**/api/v1/audit", route => route.fulfill({json: {events: [{
    request_id: requestId,
    actor: "admin",
    action: "recipe.start",
    authority_revision: "a".repeat(64),
    occurred_at: "2026-08-24T08:55:00Z",
    targets: [`spk_${"1".repeat(32)}`],
  }]}}));
  await page.route("**/api/v1/jobs?*", route => route.fulfill({json: {
    jobs: [{id: "operation-install", kind: "recipe-install", state: "running", created_at: "2026-08-24T08:58:00Z"}],
    next_cursor: null,
    total: 1,
  }}));

  await page.goto("/activity");

  await expect(page.getByRole("heading", {name: "Activity"})).toBeVisible();
  await expect(page.getByRole("heading", {name: "Started recipe"})).toBeVisible();
  await expect(page.getByRole("heading", {name: "Recipe Install · Running"})).toBeVisible();
  await expect(page.getByText(requestId)).toBeHidden();
  await page.getByRole("button", {name: /admin/i}).click();
  await expect(page.getByRole("menu", {name: "Operator menu"})).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});
