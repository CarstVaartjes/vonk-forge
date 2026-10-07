import {expect, test, type Page} from "@playwright/test";
import type {components} from "../src/api/generated";

const key = {name: "laptop", models: [], created_at: "2026-09-01T10:00:00Z", last_used_at: null, expires_at: null};

async function openKeys(page: Page) {
  let revoked = false;
  await page.addInitScript(() => { document.cookie = "vonk_csrf=e2e-csrf; Path=/; SameSite=Strict"; });
  await page.route("**/api/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
  await page.route("**/api/key", route => route.fulfill({json: {keys: revoked ? [] : [key]}}));
  await page.route("**/api/key/laptop/revoke", route => { revoked = true; return route.fulfill({json: {name: "laptop"}}); });
  await page.goto("/keys");
  await page.locator("tbody tr").first().getByRole("button").last().click();
}

test("an irreversible action asks for the name, blocks the page behind it, and ends in a toast", async ({page}) => {
  await openKeys(page);
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await expect(page.locator("#root")).toHaveJSProperty("inert", true);

  const confirm = dialog.locator("button[type=submit]");
  await expect(confirm).toBeDisabled();
  await dialog.getByRole("textbox").fill("not-the-name");
  await expect(confirm).toBeDisabled();
  await dialog.getByRole("textbox").fill("laptop");
  await expect(confirm).toBeEnabled();
  await confirm.click();

  await expect(dialog).toHaveCount(0);
  await expect(page.locator("#root")).toHaveJSProperty("inert", false);
  await expect(page.locator(".toast-region .toast[data-kind=success]")).toHaveCount(1);
});

test("a failed action reports a toast and keeps the row", async ({page}) => {
  await openKeys(page);
  const refusal: components["schemas"]["BoundedErrorResponse"] = {detail: "key storage unavailable"};
  await page.route("**/api/key/laptop/revoke", route => route.fulfill({status: 503, headers: {"x-request-id": "req-e2e-1"}, json: refusal}));
  const dialog = page.getByRole("alertdialog");
  await dialog.getByRole("textbox").fill("laptop");
  await dialog.locator("button[type=submit]").click();

  const toast = page.locator(".toast-region .toast[data-kind=error]");
  await expect(toast).toHaveCount(1);
  await expect(toast).toContainText("req-e2e-1");
  await expect(toast).toContainText(refusal.detail);
  await expect(page.locator("tbody tr")).toHaveCount(1);
});

test("expired cookies return a non-polling page to sign-in before mutation", async ({page}) => {
  // Break caught: fail-fast CSRF validation bypasses AuthProvider when both
  // cookies expire, stranding Keys until the operator reloads the page.
  await openKeys(page);
  let mutations = 0;
  await page.route("**/api/key/laptop/revoke", route => { mutations += 1; return route.fulfill({json: {name: "laptop"}}); });
  await page.route("**/api/auth/session", route => route.fulfill({status: 401, json: {detail: "authentication failed"}}));
  await page.context().clearCookies();
  const dialog = page.getByRole("alertdialog");
  await dialog.getByRole("textbox").fill("laptop");
  await dialog.locator("button[type=submit]").click();
  await expect(page.getByRole("heading", {name: "Sign in", exact: true})).toBeVisible();
  expect(mutations).toBe(0);
});
