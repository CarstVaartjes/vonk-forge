import AxeBuilder from "@axe-core/playwright";
import {expect, test, type Page, type TestInfo} from "@playwright/test";

const commit = "a".repeat(40);

async function expectNoSeriousAccessibilityViolations(page: Page) {
  const results = await new AxeBuilder({page}).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations, results.violations.map(value => `${value.id}: ${value.help}`).join("\n")).toEqual([]);
}

async function expectNoDocumentOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
}

async function attachScreenshot(page: Page, testInfo: TestInfo, name: string) {
  await testInfo.attach(name, {body: await page.screenshot({fullPage: true}), contentType: "image/png"});
}

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
  await expect(page.locator("h1")).toHaveCount(1);
  const primaryLinks = page.getByRole("navigation", {name: "Primary"}).getByRole("link");
  await expect(primaryLinks).toHaveText(["Fleet", "Library", "Activity"]);
  await expect(page).toHaveURL(/\/library$/);

  for (const width of [760, 761, 768, 864, 865]) {
    await page.setViewportSize({width, height: 900});
    const toggle = page.getByRole("button", {name: "Open system navigation"});
    if (width <= 864) {
      await expect(toggle).toBeVisible();
      await expect(page.locator(".app-sidebar")).toHaveCSS("position", "sticky");
      await toggle.click();
      const navigationDialog = page.getByRole("dialog", {name: "Navigation"});
      await expect(navigationDialog).toHaveAttribute("aria-modal", "true");
      await expect(page.locator("main")).toHaveAttribute("inert", "");
      await expect(primaryLinks.first()).toBeFocused();
      await primaryLinks.first().press("Shift+Tab");
      await expect(page.getByRole("button", {name: "Close system navigation"})).toBeFocused();
      await page.keyboard.press("Shift+Tab");
      await expect(page.getByRole("button", {name: /admin/i})).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(page.getByRole("button", {name: "Open system navigation"})).toBeFocused();
      await expect(primaryLinks.first()).toBeHidden();
    } else {
      await expect(toggle).toBeHidden();
      await expect(primaryLinks.first()).toBeVisible();
    }
    await expectNoDocumentOverflow(page);
  }
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
  await expect(page.getByRole("group", {name: "Operator actions"})).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expectNoSeriousAccessibilityViolations(page);
  for (const width of [320, 360, 768, 1280]) {
    await page.setViewportSize({width, height: width <= 360 ? 800 : 900});
    await expectNoDocumentOverflow(page);
  }
});

test("the sign-in screen remains focused, accessible, and usable on small screens", async ({page}) => {
  await page.route("**/api/v1/auth/session", route => route.fulfill({status: 401, json: {detail: "Authentication required"}}));
  await page.goto("/fleet");
  await expect(page.getByRole("heading", {name: "Sign in"})).toBeVisible();
  await expect(page.getByRole("textbox", {name: "Administrator account"})).toHaveAttribute("autocomplete", "username");
  await expect(page.getByLabel("Password")).toHaveAttribute("autocomplete", "current-password");
  await expect(page.getByLabel("Password")).toBeFocused();
  for (const width of [320, 1280]) {
    await page.setViewportSize({width, height: width === 320 ? 700 : 900});
    await expectNoDocumentOverflow(page);
    await expectNoSeriousAccessibilityViolations(page);
  }
});

test("the custom recipe builder guides a complete, responsive, accessible creation", async ({page}, testInfo) => {
  await page.route("**/api/v1/catalog/recipes", async route => {
    if (route.request().method() !== "POST") return route.fallback();
    const request = route.request().postDataJSON() as {slug: string; document: Record<string, unknown>};
    await route.fulfill({json: {recipe_id: "custom-1", revision_number: 1, lifecycle: "draft", slug: request.slug, document: request.document}});
  });
  await page.goto("/library/create");
  await expect(page.getByRole("heading", {name: "Create custom recipe"})).toBeVisible();
  await expect(page.locator("h1")).toHaveCount(1);
  await page.getByRole("textbox", {name: "Display name"}).fill("Protected browser draft");
  await page.getByRole("link", {name: "Fleet"}).click();
  const discard = page.getByRole("alertdialog", {name: "Discard this draft?"});
  await expect(discard).toBeVisible();
  await expect(page).toHaveURL(/\/library\/create$/);
  await discard.getByRole("button", {name: "Keep editing"}).click();
  await expect(page.getByRole("textbox", {name: "Display name"})).toHaveValue("Protected browser draft");
  await page.setViewportSize({width: 320, height: 800});
  await expectNoDocumentOverflow(page);
  await expectNoSeriousAccessibilityViolations(page);
  await page.getByRole("button", {name: "Open system navigation"}).click();
  await page.getByRole("link", {name: "Fleet"}).click();
  const mobileDiscard = page.getByRole("alertdialog", {name: "Discard this draft?"});
  await expect(mobileDiscard).toBeVisible();
  await mobileDiscard.getByRole("button", {name: "Keep editing"}).click();
  await expect(page.getByRole("link", {name: "Fleet"})).toBeFocused();
  await expect(page.getByRole("dialog", {name: "Navigation"})).toHaveAttribute("aria-modal", "true");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", {name: "Open system navigation"})).toBeFocused();

  await page.getByRole("textbox", {name: "Exact model digest"}).fill("a1".repeat(32));
  await page.getByRole("button", {name: "Continue"}).click();
  await page.getByRole("textbox", {name: "Exact harness digest"}).fill("b2".repeat(32));
  await page.getByRole("textbox", {name: "Exact runtime digest"}).fill("c3".repeat(32));
  await page.getByRole("textbox", {name: "Exact build context digest"}).fill("d4".repeat(32));
  await page.getByRole("button", {name: "Continue"}).click();
  await expect(page.getByRole("heading", {name: "Artifacts"})).toBeVisible();
  await page.getByRole("textbox", {name: "Immutable revision"}).fill("e5".repeat(20));
  for (const heading of ["Resources & topology", "Validation & provenance", "Review & create"]) {
    await page.getByRole("button", {name: "Continue"}).click();
    await expect(page.getByRole("heading", {name: heading})).toBeVisible();
  }

  await page.setViewportSize({width: 1280, height: 900});
  await expect(page.getByRole("region", {name: "Recipe builder review"})).toBeVisible();
  await expect(page.getByRole("heading", {name: "Draft preflight"})).toBeVisible();
  await expect(page.getByText("Not uploaded by this builder")).toBeVisible();
  await expectNoDocumentOverflow(page);
  await expectNoSeriousAccessibilityViolations(page);
  await attachScreenshot(page, testInfo, "custom-recipe-review-desktop.png");
  await page.getByRole("button", {name: "Save recipe draft"}).click();
  await expect(page.getByText("Recipe draft saved")).toBeVisible();
  await expect(page.getByRole("button", {name: "View saved draft"})).toBeVisible();
});
