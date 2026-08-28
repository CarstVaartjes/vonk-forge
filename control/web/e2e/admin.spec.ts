import AxeBuilder from "@axe-core/playwright";
import {expect, test, type Page, type TestInfo} from "@playwright/test";

const commit = "a".repeat(40);

async function expectNoSeriousAccessibilityViolations(page: Page) {
  const results = await new AxeBuilder({page}).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations, results.violations.map(value => `${value.id}: ${value.help}`).join("\n")).toEqual([]);
}

async function expectNoDocumentOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() => {
    const viewport = document.documentElement.clientWidth;
    if (document.documentElement.scrollWidth <= viewport) return [];
    return [...document.querySelectorAll("body *")]
      .map(element => {
        const rect = element.getBoundingClientRect();
        const path: string[] = [];
        for (let current: Element | null = element; current && current !== document.body; current = current.parentElement) {
          const classes = current.getAttribute("class")?.trim().replace(/\s+/g, ".");
          path.unshift(`${current.tagName.toLowerCase()}${classes ? `.${classes}` : ""}`);
        }
        return {
          path: path.join(" > "),
          text: element.textContent?.trim().replace(/\s+/g, " ").slice(0, 96) || "",
          left: Math.round(rect.left),
          right: Math.round(rect.right),
          width: Math.round(rect.width),
          clientWidth: element.clientWidth,
          scrollWidth: element.scrollWidth,
        };
      })
      .filter(element => element.right > viewport + 0.5)
      .slice(0, 8);
  })).toEqual([]);
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
  await expect(primaryLinks).toHaveText(["Fleet", "Library"]);
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
  const targetId = `spk_${"1".repeat(32)}`;
  const expectedBinary = "b".repeat(64);
  const expectedBuild = `sha256:${"c".repeat(64)}`;
  let detailRequests = 0;
  await page.route("**/api/v1/audit", route => route.fulfill({json: {events: [{
    request_id: requestId,
    actor: "admin",
    action: "recipe.start",
    authority_revision: "a".repeat(64),
    occurred_at: "2026-08-24T08:55:00Z",
    targets: [targetId],
  }]}}));
  await page.route("**/api/v1/jobs?*", route => route.fulfill({json: {
    jobs: [{id: "upgrade-1", kind: "agent-upgrade", state: "waiting-for-operator", created_at: "2026-08-24T08:58:00Z"}],
    next_cursor: null,
    total: 1,
  }}));
  await page.route("**/api/v1/jobs/upgrade-1?*", route => {
    detailRequests += 1;
    return route.fulfill({json: {
      id: "upgrade-1",
      kind: "agent-upgrade",
      state: "waiting-for-operator",
      authority_revision: "a".repeat(64),
      targets: [targetId],
      target_next_cursor: null,
      target_total: 1,
      current_attempt: 1,
      status_reason: "agent upgrade helper is unavailable",
      reconciliation_id: null,
      operations: [{id: "upgrade-step", graph_operation_id: null, node_id: targetId, kind: "agent.upgrade.v1", state: "waiting-for-operator", attempt: 3, progress: null, updated_at: "2026-08-24T08:59:30Z"}],
      operation_next_cursor: null,
      operation_total: 1,
      progress: {completed: 0, failed: 0, running: 0, total: 1},
      agent_upgrade_diagnostics: {
        expected_identity: {version: "0.1.0~dev.350+g15f9faf7c5bf", binary_digest: expectedBinary, build_digest: expectedBuild},
        targets: [{
          node_id: targetId,
          state: "waiting-for-operator",
          attempts: 3,
          target_proven: false,
          observed_identity: {version: "0.1.0~dev.335+glegacy", binary_digest: "d".repeat(64), build_digest: `sha256:${"e".repeat(64)}`},
          raw_reason: "agent upgrade helper is unavailable",
          retry_not_before: "2026-08-24T09:03:30Z",
          retry_queued: true,
        }],
        legacy_generic_ambiguous: false,
        next_action: "Wait for the controller-managed retry behind its safety delay; it will not dispatch before the reported retry time. Do not manually resume the rollout again.",
        operator_summary: null,
      },
    }});
  });

  await page.goto("/activity");

  await expect(page.getByRole("heading", {name: "Activity"})).toBeVisible();
  await expect(page.getByRole("heading", {name: "Started recipe"})).toBeVisible();
  await expect(page.getByRole("heading", {name: "Agent Upgrade · Waiting for operator"})).toBeVisible();
  await page.getByText("View operation progress").click();
  await expect(page.getByText("Retry queued behind safety delay")).toBeVisible();
  await expect(page.getByText("Controller retry not before")).toBeVisible();
  await expect(page.getByText("Updates automatically while this operation is active.")).toBeVisible();
  await expect(page.getByRole("button", {name: "Queue retry after inspection"})).toHaveCount(0);
  await expect.poll(() => detailRequests, {timeout: 6_500}).toBeGreaterThanOrEqual(2);
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
