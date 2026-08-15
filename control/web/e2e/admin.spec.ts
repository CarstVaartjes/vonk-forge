import {test, expect} from "@playwright/test";

const commit = "a".repeat(40);
const digest = "d".repeat(64);
const evidenceDigest = "e".repeat(64);
const nodeId = "spk_0123456789abcdef0123456789abcdef";

test.beforeEach(async ({page}) => {
  await page.route("**/api/v1/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
});

test("admin shell is keyboard navigable", async ({page}) => {
  await page.route("**/api/v1/fleet", route => route.fulfill({json: {schema_version: 1, event_cursor: 0, generated_at: new Date().toISOString(), repository_commit: commit, nodes: []}}));
  await page.route("**/api/v1/documents?kind=profiles", route => route.fulfill({json: {commit, documents: []}}));
  await page.goto("/");
  await expect(page.getByRole("navigation", {name: "Primary"})).toBeVisible();
  await page.locator("summary").filter({hasText: "System"}).focus();
  await page.keyboard.press("Enter");
  await page.getByRole("link", {name: "Profiles"}).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", {name: "Profiles"})).toBeVisible();
});

test("profile apply confirms and posts the exact server digest", async ({page}) => {
  const bodies: unknown[] = [];
  await page.route("**/api/v1/documents?kind=profiles", route => route.fulfill({json: {commit, documents: []}}));
  await page.route("**/api/v1/nodes/status", route => route.fulfill({json: {
    commit, evidence_digest: evidenceDigest,
    nodes: [{agent_online: true, agent_state: "active", compatibility: "supported", disk_available_bytes: 1, display_name: "Compute A", healthy: true, hostname: "hidden.invalid", id: nodeId, labels: {}, lifecycle: "managed", memory_available_bytes: 1, profile: "production", stale: false}],
  }}));
  await page.route("**/api/v1/profiles/production/plan", route => route.fulfill({json: {
    agent_protocol_range: [3, 4], commit, digest, fleet_evidence_digest: evidenceDigest, input_digests: {},
    operation_graph: {base_commit: commit, nodes: [], schema_version: 1, targets: [nodeId]},
    placements: {}, reconciliation_id: "reconciliation-1", releases: {}, routes: {}, targets: [nodeId],
  }}));
  await page.route("**/api/v1/reconciliations", async route => {
    bodies.push(route.request().postDataJSON());
    await route.fulfill({status: 202, json: {base_commit: commit, job_id: "job-1", reconciliation_id: "reconciliation-1", state: "queued"}});
  });

  await page.goto("/profiles");
  await page.getByLabel("Profile ID to reconcile").fill("production");
  await page.getByRole("button", {name: "Preview exact plan"}).click();
  await expect(page.getByText(digest)).toBeVisible();
  await page.getByLabel(/Type the exact plan digest/).fill(digest);
  await page.getByRole("button", {name: "Apply exact plan"}).click();

  await expect(page.getByRole("status")).toContainText("job-1");
  expect(bodies).toEqual([{fleet_evidence_digest: evidenceDigest, plan_digest: digest}]);
});

test("packages remain usable at a mobile viewport", async ({page}) => {
  await page.setViewportSize({width: 390, height: 844});
  await page.route("**/api/v1/packages/families**", route => route.fulfill({json: {families: [], total: 0}}));
  await page.route("**/api/v1/packages/candidates**", route => route.fulfill({json: {candidates: [], total: 0}}));
  await page.route("**/api/v1/packages/inventory**", route => route.fulfill({json: {nodes: [], total: 0}}));
  await page.goto("/packages");
  await expect(page.getByRole("heading", {name: "Workload packages"})).toBeVisible();
  await page.getByRole("button", {name: "Open system navigation"}).click();
  await expect(page.getByRole("navigation", {name: "Primary"})).toBeVisible();
  await expect(page.locator(".shell")).toHaveCSS("grid-template-columns", "390px");
});
