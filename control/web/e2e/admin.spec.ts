import {expect, test} from "@playwright/test";

const commit = "a".repeat(40);

test.beforeEach(async ({page}) => {
  await page.route("**/api/v1/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
});

test("the redesigned shell exposes only Fleet and Library", async ({page}) => {
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
  await expect(primaryLinks).toHaveText(["Fleet", "Library"]);
  await expect(page).toHaveURL(/\/library$/);
});
