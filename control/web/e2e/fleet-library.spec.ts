import {expect, test, type Page} from "@playwright/test";

const GIB = 1024 ** 3;
const nodeId = "spk_0123456789abcdef0123456789abcdef";
const commit = "a".repeat(40);
const browserProblems = new WeakMap<Page, string[]>();

function telemetry(observedAt: string, sequence = 4) {
  return {
    id: `00000000-0000-4000-8000-${String(sequence).padStart(12, "0")}`,
    node_id: nodeId,
    boot_id: "00000000-0000-4000-8000-000000000001",
    sequence,
    observed_at: observedAt,
    received_at: observedAt,
    cpu_utilization_percent: 24.5,
    load_average_1m: 1.25,
    memory_total_bytes: 128 * GIB,
    memory_available_bytes: 92 * GIB,
    disk_total_bytes: 500 * GIB,
    disk_free_bytes: 320 * GIB,
    gpu_utilization_percent: 61,
    gpu_memory_total_bytes: 128 * GIB,
    gpu_memory_free_bytes: 84 * GIB,
    temperature_c: 43.5,
    power_watts: 22.25,
    network_receive_bytes_per_second: 2 * 1024 ** 2,
    network_transmit_bytes_per_second: 512 * 1024,
    gap_samples: 0,
    details: {accelerator_name: "NVIDIA GB10", accelerator_performance_state: "P0"},
  };
}

function localSnapshot() {
  const observedAt = new Date().toISOString();
  return {
    schema_version: 1,
    event_cursor: 12,
    generated_at: observedAt,
    repository_commit: commit,
    nodes: [{
      id: nodeId,
      display_name: "Aurora",
      hostname: "aurora.fixture.invalid",
      lifecycle: "managed",
      labels: {role: "inference"},
      connection: {agent_state: "active", certificate_state: "valid", online_state: "online", offline_reason: null, last_seen_at: observedAt, last_seen_age_seconds: 0},
      inventory: null,
      telemetry: {age_seconds: 0, freshness: "live", sample: telemetry(observedAt)},
      installed: [{
        installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", profile_name: "pair", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, "spk_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"], rank: 0, role: "leader", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
      }],
      loaded: [{
        run_id: "run-chat", installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", alias: "chat", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, "spk_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"], rank: 0, role: "leader", rank_state: "running", rank_age_seconds: 1, rank_fresh: true, run_state: "running", route_state: "published", group_state: "healthy", healthy: true, degraded_reason: null,
      }],
      reservations: {disk_bytes: 2 * GIB, unified_memory_bytes: 4 * GIB, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 1},
      warnings: [],
    }, {
      id: "spk_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      display_name: "Borealis",
      hostname: "borealis.fixture.invalid",
      lifecycle: "managed",
      labels: {role: "inference"},
      connection: {agent_state: "inactive", certificate_state: "expired", online_state: "offline", offline_reason: "certificate-expired", last_seen_at: null, last_seen_age_seconds: null},
      inventory: null,
      telemetry: null,
      installed: [],
      loaded: [],
      reservations: {disk_bytes: 0, unified_memory_bytes: 0, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 0},
      warnings: [{code: "node.offline", detail: "Certificate renewal is required.", severity: "error"}],
    }],
  };
}

async function installLocalFleetFixture(page: Page) {
  const snapshot = localSnapshot();
  await page.route("**/api/v1/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
  await page.route("**/api/v1/updates/skew", route => route.fulfill({json: {prompt_required: false}}));
  await page.route("**/api/v1/fleet/stream", route => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    headers: {"Cache-Control": "no-cache"},
    body: `retry: 60000\nid: ${snapshot.event_cursor}\nevent: fleet-snapshot\ndata: ${JSON.stringify({schema_version: 1, reset_reason: "initial", snapshot})}\n\n`,
  }));
  await page.route("**/api/v1/fleet", route => route.fulfill({json: snapshot}));
  await page.route("**/api/v1/nodes/*/telemetry?*", route => {
    const url = new URL(route.request().url());
    const start = url.searchParams.get("start") ?? snapshot.generated_at;
    const end = url.searchParams.get("end") ?? snapshot.generated_at;
    const maximumPoints = Number(url.searchParams.get("maximum_points") ?? 360);
    const first = telemetry(start, 1);
    const last = {...telemetry(end, 2), gpu_utilization_percent: 72, temperature_c: 45};
    return route.fulfill({json: {schema_version: 1, node_id: nodeId, start, end, maximum_points: maximumPoints, points: [first, last]}});
  });
}

test.beforeEach(async ({page}) => {
  const problems: string[] = [];
  browserProblems.set(page, problems);
  page.on("console", message => {
    if (["error", "warning"].includes(message.type())) problems.push(`${message.type()}: ${message.text()}`);
  });
  page.on("pageerror", error => problems.push(`pageerror: ${error.message}`));
  await installLocalFleetFixture(page);
});

test.afterEach(async ({page}) => {
  expect(browserProblems.get(page)).toEqual([]);
});

test("Fleet cards and bounded history are keyboard-accessible with local evidence", async ({page}) => {
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto("/fleet");

  await expect(page.getByRole("heading", {name: "Fleet", exact: true})).toBeVisible();
  await expect(page.getByRole("region", {name: "Fleet summary"})).toContainText("1 loaded recipe");
  const aurora = page.getByRole("article", {name: /Aurora — (Live|Delayed)/});
  await expect(aurora).toContainText("NVIDIA GB10 · P0");
  await expect(aurora.getByRole("region", {name: "Loaded recipes on Aurora"})).toContainText("Healthy · 2 of 2 ranks");
  await expect(aurora.getByRole("region", {name: "Installed recipes on Aurora"})).toContainText("Complete · 2 of 2 ranks");
  await expect(page.getByRole("article", {name: "Borealis — Offline"})).toContainText("Certificate expired");

  const detailButton = aurora.getByRole("button", {name: "View Aurora details"});
  await detailButton.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("complementary", {name: "Aurora details"})).toBeVisible();
  await expect(page.getByRole("button", {name: "Close Aurora details"})).toBeFocused();
  await expect(page.getByRole("img", {name: "Aurora GPU utilization history"})).toHaveAccessibleDescription(/2 reported samples/);
  await page.getByRole("button", {name: "24 hours"}).click();
  await expect(page.getByRole("button", {name: "24 hours"})).toHaveAttribute("aria-pressed", "true");
});

test("Fleet has no document overflow from phone through large desktop", async ({page}) => {
  await page.goto("/fleet");
  await page.getByRole("button", {name: "View Aurora details"}).click();

  for (const width of [360, 768, 1280, 1920]) {
    await page.setViewportSize({width, height: width === 360 ? 800 : 900});
    await expect.poll(() => page.evaluate(() => ({
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))).toEqual({body: width, document: width, viewport: width});
  }

  await page.setViewportSize({width: 360, height: 800});
  await expect(page.locator(".node-detail")).toHaveCSS("position", "static");
  await page.setViewportSize({width: 1920, height: 900});
  await expect(page.locator(".node-detail")).toHaveCSS("position", "sticky");
  await page.getByRole("button", {name: "Close Aurora details"}).click();
  const columns = await page.locator(".node-grid").evaluate(element => getComputedStyle(element).gridTemplateColumns.split(" ").length);
  expect(columns).toBeGreaterThanOrEqual(2);
});
