import AxeBuilder from "@axe-core/playwright";
import {expect, test} from "@playwright/test";

for (const width of [1440, 390]) {
  test(`operation progress preserves independent Spark phases at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 1000});
    const now = new Date().toISOString();
    const node = (index: number) => `spk_${String(index).repeat(32)}`;
    const progress = {phase: "copying", completed_bytes: 42_000_000, total_bytes: 168_000_000, total_bytes_known: true, bytes_per_second: 10_000_000, smoothed_bytes_per_second: 9_000_000, eta_seconds: 14, elapsed_seconds: 62, last_progress_at: now, observed_at: now, activity: "active"};
    await page.route("**/api/v1/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
    await page.route("**/api/v1/audit", route => route.fulfill({json: {events: []}}));
    await page.route("**/api/v1/fleet", route => route.fulfill({json: {schema_version: 1, event_cursor: 0, generated_at: now, authority_revision: "a".repeat(64), nodes: []}}));
    await page.route("**/api/v1/operations?*", route => route.fulfill({json: {schema_version: 2, operations: [], total: 0, next_cursor: null}}));
    await page.route("**/api/v1/jobs?*", route => route.fulfill({json: {jobs: [{id: "transfer-1", kind: "reconcile", state: "running", created_at: now}], total: 1, next_cursor: null}}));
    await page.route("**/api/v1/jobs/transfer-1?*", route => route.fulfill({json: {
      id: "transfer-1", kind: "reconcile", state: "running", authority_revision: "a".repeat(64), targets: [node(1), node(2)], target_total: 2, target_next_cursor: null,
      current_attempt: 1, status_reason: null, operation_total: 2, operation_next_cursor: null,
      progress: {completed: 0, failed: 0, running: 2, total: 2, operation: {...progress, phase: "prepare", total_bytes: null, total_bytes_known: false, eta_seconds: null}},
      operations: [{id: "step-1", node_id: node(1), kind: "artifact.distribution.v1", state: "running", attempt: 1, updated_at: now, progress}, {id: "step-2", node_id: node(2), kind: "artifact.distribution.v1", state: "running", attempt: 1, updated_at: now, progress: {...progress, phase: "verifying", activity: "waiting", last_progress_at: "2026-01-01T00:00:00Z"}}],
    }}));
    await page.goto("/activity");
    await page.getByText("View operation progress").click();
    await expect(page.getByRole("progressbar", {name: "Copying transfer"})).toHaveAttribute("aria-valuenow", "42000000");
    await expect(page.getByRole("progressbar", {name: "Verifying transfer"})).not.toHaveAttribute("aria-valuenow");
    await expect(page.getByText("Waiting for progress")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const result = await new AxeBuilder({page}).withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(result.violations).toEqual([]);
    await page.screenshot({path: `/private/tmp/vonk-issues-20260908/issue-594-progress-${width}.png`, fullPage: true});
  });
}
