import AxeBuilder from "@axe-core/playwright";
import {expect, test, type Page} from "@playwright/test";

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

test.beforeEach(async ({page}) => {
  await page.route("**/api/v1/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
});

test("the redesigned shell exposes the focused workspace routes", async ({page}) => {
  await page.route("**/api/v1/fleet", route => route.fulfill({json: {schema_version: 1, event_cursor: 0, generated_at: new Date().toISOString(), authority_revision: commit, nodes: []}}));
  await page.route("**/api/v1/library**", route => route.fulfill({json: {
    schema_version: 2,
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
    await expect(primaryLinks.first()).toBeVisible();
    await expect(page.getByRole("button", {name: /admin/i})).toBeVisible();
    await expect(page.getByRole("button", {name: "Open system navigation"})).toHaveCount(0);
    await expectNoDocumentOverflow(page);
  }
});

test("Activity combines friendly audit history and current operations", async ({page}) => {
  const requestId = "f6e73ce3-3329-4ff4-b086-d8f87c879ce9";
  const targetId = `spk_${"1".repeat(32)}`;
  const expectedBinary = "b".repeat(64);
  const expectedBuild = `sha256:${"c".repeat(64)}`;
  let detailRequests = 0;
  await page.route("**/api/v1/operations?*", route => route.fulfill({json: {schema_version: 2, operations: [], total: 0, next_cursor: null}}));
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
      operations: [{id: "upgrade-step", node_id: targetId, kind: "agent.upgrade.v1", state: "waiting-for-operator", attempt: 3, progress: null, updated_at: "2026-08-24T08:59:30Z"}],
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

for (const scenario of [
  {kind: "fleet-profile.apply", endpoint: "fleet-profile-applications", label: "Fleet Profile Apply", width: 1280},
  {kind: "library.placement", endpoint: "library/placements", label: "Library Placement", width: 360},
]) {
  test(`Activity retries canonical ${scenario.kind} with the same key and follows the linked attempt`, async ({page}) => {
    const originalId = "11111111-1111-4111-8111-111111111111";
    const retryId = "22222222-2222-4222-8222-222222222222";
    const requests: Array<{request_key: string}> = [];
    let detailRequests = 0;
    const failed = {
      schema_version: 2, id: originalId, parent_id: null, kind: scenario.kind,
      state: "failed", attempt: 1, node_ids: [], created_at: "2026-09-07T12:00:00Z",
      progress: {phase: "prepare", completed_bytes: 0, total_bytes_known: false},
      failure: {error_code: "child_operation_failed", summary: "Image installation failed", detail: "Verification failed on Studio Spark; completed work is retained.", retryable: true, uncertain: true},
      recovery: {uncertain: true, actions: ["inspect", "retry"], explanation: "Inspect installed state before retrying."},
    };
    await page.setViewportSize({width: scenario.width, height: 900});
    await page.route("**/api/v1/audit", route => route.fulfill({json: {events: []}}));
    await page.route("**/api/v1/jobs?*", route => route.fulfill({json: {jobs: [], total: 0, next_cursor: null}}));
    await page.route("**/api/v1/fleet", route => route.fulfill({json: {schema_version: 1, event_cursor: 0, generated_at: "2026-09-07T12:00:00Z", authority_revision: commit, nodes: []}}));
    await page.route("**/api/v1/library?*", route => route.fulfill({json: {
      schema_version: 2, generated_at: "2026-09-07T12:00:00Z",
      freshness_policy: {inventory_fresh_seconds: 300, telemetry_live_seconds: 6, telemetry_delayed_seconds: 20},
      models: [], unlinked_recipes: [], next_cursor: null,
    }}));
    await page.route("**/api/v1/operations?*", route => route.fulfill({json: {schema_version: 2, operations: [failed], total: 1, next_cursor: null}}));
    await page.route(`**/api/v1/${scenario.endpoint}/${originalId}/retry`, route => {
      expect(route.request().method()).toBe("POST");
      requests.push(route.request().postDataJSON());
      if (requests.length === 1) return route.abort("failed");
      return route.fulfill({json: {schema_version: 1, id: retryId, retry_of_application_id: originalId, attempt: 2, state: "running"}});
    });
    await page.route(`**/api/v1/operations/${retryId}`, route => {
      detailRequests += 1;
      return route.fulfill({json: {
        ...failed, id: retryId, parent_id: originalId, attempt: 2,
        state: detailRequests === 1 ? "running" : "succeeded",
        failure: null, recovery: {actions: ["inspect"], uncertain: false},
        progress: {phase: detailRequests === 1 ? "prepare" : "final_verify", completed_bytes: 0, total_bytes_known: false},
      }});
    });
    await page.goto("/activity");
    await expect(page.getByRole("heading", {name: `${scenario.label} · Failed`})).toBeVisible();
    await expect(page.getByText("Image installation failed", {exact: true})).toBeVisible();
    await expect(page.getByText("Verification failed on Studio Spark; completed work is retained.")).toBeVisible();
    await expect(page.getByText("Attempt 1 · Prepare", {exact: true})).toBeVisible();
    await expect(page.getByText(/Outcome uncertain/)).toBeVisible();
    await page.getByRole("button", {name: "Retry operation", exact: true}).click();
    await expect(page.getByRole("alert")).toContainText("Retry response could not be confirmed");
    expect(requests).toHaveLength(1);
    expect(requests[0].request_key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    await page.getByRole("button", {name: "Retry operation", exact: true}).click();
    await expect(page.getByRole("heading", {name: `${scenario.label} · Running`})).toBeVisible();
    expect(requests).toHaveLength(2);
    expect(requests[1]).toEqual(requests[0]);
    await expect(page.getByText("Attempt 2 · Prepare · Updates automatically", {exact: true})).toBeVisible();
    await expect(page.getByRole("button", {name: "Retry operation", exact: true})).toBeDisabled();
    await expect(page.getByRole("heading", {name: `${scenario.label} · Completed`})).toBeVisible({timeout: 7_000});
    expect(detailRequests).toBeGreaterThanOrEqual(2);
    await expect(page.getByRole("heading", {name: `${scenario.label} · Failed`})).toBeVisible();
    await expectNoDocumentOverflow(page);
    await expectNoSeriousAccessibilityViolations(page);
  });
}
