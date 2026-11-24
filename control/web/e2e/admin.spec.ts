import { expect, test, type Page } from "@playwright/test";
import type { components } from "../src/api/generated";
import { expectNoSeriousAccessibilityViolations, expectReachableByKeyboard } from "./a11y";
import { serveEmptyFleet, serveOverviewFleet } from "./fleet-fixture";
import { fixtureModelPath, fixtureRecipeTitle, serveLibrary } from "./library-fixture";

async function expectNoDocumentOverflow(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(() => {
        const viewport = document.documentElement.clientWidth;
        if (document.documentElement.scrollWidth <= viewport) return [];
        return [...document.querySelectorAll("body *")]
          .map((element) => {
            const rect = element.getBoundingClientRect();
            const path: string[] = [];
            for (
              let current: Element | null = element;
              current && current !== document.body;
              current = current.parentElement
            ) {
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
          .filter((element) => element.right > viewport + 0.5)
          .slice(0, 8);
      }),
    )
    .toEqual([]);
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/auth/session", (route) =>
    route.fulfill({
      json: { subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z" },
    }),
  );
});

test("the redesigned shell exposes the focused workspace routes", async ({ page }) => {
  await serveEmptyFleet(page);
  await page.route("**/api/model/library**", (route) =>
    route.fulfill({
      json: {
        schema_version: 2,
        generated_at: new Date().toISOString(),
        freshness_policy: {
          inventory_fresh_seconds: 300,
          telemetry_live_seconds: 6,
          telemetry_delayed_seconds: 20,
        },
        facets: { family: [], line: [], quantization: [], usage: [], version: [] },
        filters: {},
        models: [],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/api/recipe/library**", (route) =>
    route.fulfill({
      json: {
        schema_version: 2,
        generated_at: new Date().toISOString(),
        freshness_policy: {
          inventory_fresh_seconds: 300,
          telemetry_live_seconds: 6,
          telemetry_delayed_seconds: 20,
        },
        facets: { family: [], line: [], quantization: [], usage: [], version: [] },
        filters: {},
        recipes: [],
        next_cursor: null,
      },
    }),
  );
  await page.goto("/library");
  await expect(page.getByRole("heading", { name: "Library", exact: true })).toBeVisible();
  await expect(page.locator("h1")).toHaveCount(1);
  const primaryLinks = page.getByRole("navigation", { name: "Primary" }).getByRole("link");
  await expect(primaryLinks).toHaveText(["Overview", "Workloads", "Library", "Activity"]);
  await expect(page).toHaveURL(/\/library$/);

  for (const width of [760, 761, 768, 864, 865]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(primaryLinks.first()).toBeVisible();
    await expect(page.getByRole("button", { name: /admin/i })).toBeVisible();
    await expect(page.getByRole("button", { name: "Open system navigation" })).toHaveCount(0);
    await expectNoDocumentOverflow(page);
  }
});

test("Fleet status line reads as one sentence at narrow and wide widths", async ({ page }) => {
  await serveEmptyFleet(page);
  await page.goto("/fleet");
  await expect(page.locator(".fleet-status-line")).toHaveText("No Sparks are enrolled.");
  await expect(page.getByRole("heading", { name: "Fleet unavailable" })).toHaveCount(0);

  for (const width of [360, 768, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await expectNoDocumentOverflow(page);
  }
});

test("Overview fits a phone and a desktop and passes the accessibility checks", async ({
  page,
}) => {
  await serveOverviewFleet(page);
  await page.goto("/overview");
  await expect(page.getByRole("heading", { name: "GLM 5.3 Flash" })).toBeVisible();
  await expect(page.getByRole("region", { name: "spark-3542" })).toBeVisible();
  for (const colorScheme of ["dark", "light"] as const) {
    await page.emulateMedia({ colorScheme });
    for (const [width, height] of [
      [400, 800],
      [1440, 900],
    ] as const) {
      await page.setViewportSize({ width, height });
      await expectNoDocumentOverflow(page);
      await expectNoSeriousAccessibilityViolations(page);
    }
  }
  await page.setViewportSize({ width: 400, height: 800 });
  for (const link of await page.getByRole("link", { name: /^spark-/ }).all()) {
    expect((await link.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  }
});

test("Workloads fits a phone and a desktop, opened or not, and passes the accessibility checks", async ({
  page,
}) => {
  await serveOverviewFleet(page);
  await page.goto("/workloads");
  const toggle = page.getByRole("button", { name: "GLM 5.3 Flash" });
  await expect(toggle).toBeVisible();
  for (const open of [false, true]) {
    if (open) await toggle.click();
    for (const colorScheme of ["dark", "light"] as const) {
      await page.emulateMedia({ colorScheme });
      for (const [width, height] of [
        [400, 800],
        [1440, 900],
      ] as const) {
        await page.setViewportSize({ width, height });
        await expectNoDocumentOverflow(page);
        await expectNoSeriousAccessibilityViolations(page);
      }
    }
  }
});

test("a Spark page fits a phone and a desktop and passes the accessibility checks", async ({
  page,
}) => {
  await serveOverviewFleet(page);
  await page.goto("/overview");
  await page.getByRole("link", { name: "spark-3542" }).first().click();
  await expect(page).toHaveURL(/\/sparks\/spk_/);
  await expect(page.getByRole("heading", { name: "spark-3542", level: 1 })).toBeVisible();
  for (const colorScheme of ["dark", "light"] as const) {
    await page.emulateMedia({ colorScheme });
    for (const [width, height] of [
      [400, 800],
      [1440, 900],
    ] as const) {
      await page.setViewportSize({ width, height });
      await expectNoDocumentOverflow(page);
      await expectNoSeriousAccessibilityViolations(page);
    }
  }
});

test("API keys passes the accessibility checks", async ({ page }) => {
  await serveEmptyFleet(page);
  await page.route("**/api/key", (route) => route.fulfill({ json: { keys: [] } }));
  await page.goto("/keys");
  await expect(page.getByRole("heading", { name: /keys/i }).first()).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("Activity shows operations and standalone jobs", async ({ page }) => {
  await serveEmptyFleet(page);
  const requestId = "f6e73ce3-3329-4ff4-b086-d8f87c879ce9";
  const targetId = `spk_${"1".repeat(32)}`;
  const expectedBinary = "b".repeat(64);
  const expectedBuild = `sha256:${"c".repeat(64)}`;
  let detailRequests = 0;
  const operations: components["schemas"]["OperationsResponse"] = {
    operations: [
      {
        id: requestId,
        parent_id: null,
        kind: "recipe.start",
        state: "succeeded",
        summary: "Recipe start finished",
        subject: "h3video",
        attempt: 1,
        node_ids: [targetId],
        created_at: "2026-08-24T08:55:00Z",
        progress: null,
        failure: null,
        recovery: null,
      },
      {
        id: "job:upgrade-1",
        parent_id: null,
        kind: "agent-upgrade",
        state: "needs-operator",
        summary: "Agent upgrade waiting for operator",
        attempt: 1,
        node_ids: [targetId],
        created_at: "2026-08-24T08:58:00Z",
        progress: null,
        failure: null,
        recovery: null,
      },
    ],
    total: 2,
    next_cursor: null,
  };
  await page.route("**/api/operations?*", (route) => route.fulfill({ json: operations }));
  await page.route("**/api/jobs/upgrade-1?*", (route) => {
    detailRequests += 1;
    const detail: components["schemas"]["JobDetailResponse"] = {
      id: "upgrade-1",
      kind: "agent-upgrade",
      state: "needs-operator",
      summary: "Agent upgrade waiting for operator",
      authority_revision: "a".repeat(64),
      targets: [targetId],
      target_next_cursor: null,
      target_total: 1,
      current_attempt: 1,
      status_reason: "agent upgrade helper is unavailable",
      operations: [
        {
          id: "upgrade-step",
          node_id: targetId,
          kind: "agent.upgrade.v1",
          state: "needs-operator",
          summary: "Agent upgrade waiting for operator",
          attempt: 3,
          progress: null,
          updated_at: "2026-08-24T08:59:30Z",
        },
      ],
      operation_next_cursor: null,
      operation_total: 1,
      progress: { completed: 0, failed: 0, running: 0, total: 1 },
      agent_upgrade_diagnostics: {
        expected_identity: {
          version: "0.1.0~dev.350+g15f9faf7c5bf",
          binary_digest: expectedBinary,
          build_digest: expectedBuild,
        },
        targets: [
          {
            node_id: targetId,
            state: "needs-operator",
            attempts: 3,
            target_proven: false,
            observed_identity: {
              version: "0.1.0~dev.335+glegacy",
              binary_digest: "d".repeat(64),
              build_digest: `sha256:${"e".repeat(64)}`,
            },
            raw_reason: "agent upgrade helper is unavailable",
            retry_not_before: "2026-08-24T09:03:30Z",
            retry_queued: true,
          },
        ],
        failure_details_unavailable: false,
        next_action:
          "Wait for the controller-managed retry behind its safety delay; it will not dispatch before the reported retry time. Do not manually resume the rollout again.",
        operator_summary: null,
      },
    };
    return route.fulfill({ json: detail });
  });

  await page.goto("/activity");

  await expect(page.getByRole("heading", { name: "Activity" })).toBeVisible();
  await expect(page.getByRole("heading", { name: /Recipe start finished$/ })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: /Agent upgrade waiting for operator$/ }),
  ).toBeVisible();
  await page.getByText("View operation progress").click();
  await expect(page.getByText("Retry queued behind safety delay")).toBeVisible();
  await expect(page.getByText("Controller retry not before")).toBeVisible();
  await expect(
    page.getByText("Updates automatically while this operation is active."),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Queue retry after inspection" })).toHaveCount(0);
  await expect.poll(() => detailRequests, { timeout: 6_500 }).toBeGreaterThanOrEqual(2);
  await expect(page.getByText(requestId)).toBeHidden();
  await page.getByRole("button", { name: /admin/i }).click();
  await expect(page.getByRole("group", { name: "Operator actions" })).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expectNoSeriousAccessibilityViolations(page);
  for (const width of [320, 360, 768, 1280]) {
    await page.setViewportSize({ width, height: width <= 360 ? 800 : 900 });
    await expectNoDocumentOverflow(page);
  }
});

test("the sign-in screen remains focused, accessible, and usable on small screens", async ({
  page,
}) => {
  await page.route("**/api/auth/session", (route) =>
    route.fulfill({ status: 401, json: { detail: "Authentication required" } }),
  );
  await page.goto("/fleet");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Administrator account" })).toHaveAttribute(
    "autocomplete",
    "username",
  );
  await expect(page.getByLabel("Password")).toHaveAttribute("autocomplete", "current-password");
  await expect(page.getByLabel("Password")).toBeFocused();
  for (const width of [320, 1280]) {
    await page.setViewportSize({ width, height: width === 320 ? 700 : 900 });
    await expectNoDocumentOverflow(page);
    await expectNoSeriousAccessibilityViolations(page);
  }
});

test("Library and a Recipe pass the accessibility checks and the main navigation and primary actions work by keyboard", async ({
  page,
}) => {
  await serveEmptyFleet(page);
  await serveLibrary(page);
  const nav = page.getByRole("navigation", { name: "Primary" }).getByRole("link");
  await page.goto("/library");
  await expect(
    page.getByRole("link", { name: new RegExp(fixtureRecipeTitle) }).first(),
  ).toBeVisible();
  // The page starts with focus in its content, so one Shift+Tab walk reaches every primary link.
  for (const name of ["Activity", "Library", "Workloads", "Overview"]) {
    await expectReachableByKeyboard(page, nav.filter({ hasText: name }), "Shift+Tab");
  }
  // The Model page shows a Recipe row with its cache action ("Also caches ...").
  await page.goto(fixtureModelPath);
  await expect(page.locator(".library-cache-missing").first()).toBeVisible();
  const recipeLink = page.getByRole("link", { name: new RegExp(fixtureRecipeTitle) }).first();
  for (const colorScheme of ["dark", "light"] as const) {
    await page.emulateMedia({ colorScheme });
    await expectNoSeriousAccessibilityViolations(page);
  }
  await recipeLink.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/library\/recipes\//);
  await expect(page.locator("#recipe-detail-heading")).toHaveText(fixtureRecipeTitle);
  for (const colorScheme of ["dark", "light"] as const) {
    await page.emulateMedia({ colorScheme });
    await expectNoSeriousAccessibilityViolations(page);
  }
  await expectReachableByKeyboard(
    page,
    page.getByRole("button", { name: "Refresh", exact: true }),
    "Shift+Tab",
  );
});

test("the Library Spark counts stay one scrollable row on a phone and the page does not overflow", async ({
  page,
}) => {
  await serveEmptyFleet(page);
  await serveLibrary(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/library");
  const sparks = page.getByRole("group", { name: "Sparks per recipe" });
  await expect(sparks).toBeVisible();
  const buttons = sparks.getByRole("button");
  await expect(buttons.last()).toBeAttached();
  const shape = await sparks.evaluate((group) => {
    const row = group.getBoundingClientRect();
    const heights = [...group.querySelectorAll("button")].map(
      (button) => button.getBoundingClientRect().height,
    );
    return {
      withinPage: row.right <= document.documentElement.clientWidth + 0.5,
      oneRow: Math.max(...heights) - Math.min(...heights) < 1,
      tallest: Math.max(...heights),
    };
  });
  expect(shape.withinPage).toBe(true);
  expect(shape.oneRow).toBe(true);
  expect(shape.tallest).toBeLessThan(64);
  await buttons.last().scrollIntoViewIfNeeded();
  await expect(buttons.last()).toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    ),
  ).toBe(true);
});
