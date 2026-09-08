import AxeBuilder from "@axe-core/playwright";
import {expect, test} from "@playwright/test";

for (const width of [1440, 390]) {
  test(`Fleet shows measured lifecycle phase at ${width}px`, async ({page}, testInfo) => {
    await page.setViewportSize({width, height: 1000});
    const now = new Date().toISOString();
    const id = "11111111-1111-4111-8111-111111111111";
    const nodeId = `spk_${"1".repeat(32)}`;
    const profile = {schema_version: 2, id, name: "Studio", description: "Qwen Chat on Spark One", scope: {node_ids: [nodeId]}, assignments: [], profile_digest: "a".repeat(64)};
    const progress = {phase: "copying", completed_bytes: 42_000_000, total_bytes: 168_000_000, total_bytes_known: true, smoothed_bytes_per_second: 9_000_000, eta_seconds: 14, elapsed_seconds: 62, last_progress_at: now, observed_at: now, activity: "active"};
    const application = {schema_version: 2, id, profile_id: id, profile_digest: profile.profile_digest, plan_digest: "b".repeat(64), state: "running", current_step: 0, total_steps: 2, progress: {attempt: 1, completed_steps: 0, total_steps: 2, child_progress: {phase: "target-copy", node_ids: [nodeId], bytes: progress.completed_bytes, total_bytes: progress.total_bytes, operation: progress}}, status_reason: null, created_at: now, updated_at: now};
    await page.route("**/api/v1/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
    await page.route("**/api/v1/fleet", route => route.fulfill({json: {schema_version: 1, event_cursor: 0, generated_at: now, authority_revision: "a".repeat(64), nodes: [{id: nodeId, display_name: "Spark One", hostname: "spark-one.fixture.invalid", lifecycle: "managed", labels: {}, connection: {agent_state: "active", certificate_state: "valid", online_state: "online", offline_reason: null, last_seen_at: now, last_seen_age_seconds: 0}, inventory: null, telemetry: null, installed: [], loaded: [], reservations: {disk_bytes: 0, unified_memory_bytes: 0, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 0}, warnings: []}]}}));
    await page.route("**/api/v1/fleet-profiles", route => route.fulfill({json: {schema_version: 2, profiles: [profile]}}));
    await page.route("**/api/v1/fleet-profiles/*/preview", route => route.fulfill({json: {schema_version: 2, profile_id: id, profile_name: "Studio", plan_digest: "b".repeat(64), allowed: true, steps: [{index: 0, kind: "start", label: "Run Qwen Chat", node_ids: [nodeId]}], reasons: []}}));
    await page.route("**/api/v1/fleet-profiles/*/apply", route => route.fulfill({json: application}));
    await page.route("**/api/v1/fleet-profile-applications/*", route => route.fulfill({json: application}));
    await page.goto("/fleet");
    await page.getByRole("button", {name: "Switch profile", exact: true}).click();
    await expect(page.getByText(/8.6 MiB\/s.*14s left/)).toBeVisible();
    await expect(page.getByRole("progressbar", {name: "Copying transfer"})).toHaveAttribute("aria-valuenow", "42000000");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect((await new AxeBuilder({page}).withTags(["wcag2a", "wcag2aa"]).analyze()).violations).toEqual([]);
    await page.screenshot({path: testInfo.outputPath(`issue-597-fleet-${width}.png`), fullPage: true});
  });
}
