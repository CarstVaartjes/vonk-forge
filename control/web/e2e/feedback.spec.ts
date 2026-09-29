import {expect, test, type Page} from "@playwright/test";

const key = {name: "laptop", models: [], created_at: "2026-09-01T10:00:00Z", last_used_at: null, expires_at: null};

async function openKeys(page: Page) {
  let revoked = false;
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
  await page.route("**/api/key/laptop/revoke", route => route.fulfill({status: 500, headers: {"x-request-id": "req-e2e-1"}, json: {detail: "boom"}}));
  const dialog = page.getByRole("alertdialog");
  await dialog.getByRole("textbox").fill("laptop");
  await dialog.locator("button[type=submit]").click();

  const toast = page.locator(".toast-region .toast[data-kind=error]");
  await expect(toast).toHaveCount(1);
  await expect(toast).toContainText("req-e2e-1");
  await expect(page.locator("tbody tr")).toHaveCount(1);
});
