import {expect, test, type Page} from "@playwright/test";
import {fullLibraryDetail, librarySnapshot, minimalLibraryDetail, unlinkedRecipe} from "../src/test-fixtures/library";

const GIB = 1024 ** 3;
const nodeId = "spk_0123456789abcdef0123456789abcdef";
const borealisId = "spk_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const commit = "a".repeat(40);
const browserProblems = new WeakMap<Page, string[]>();
type LibraryFixtureState = {
  detailFailuresRemaining: number;
  empty: boolean;
  lastApplyBody?: Record<string, unknown>;
  retryCount: number;
  snapshotFailuresRemaining: number;
};
const libraryFixtures = new WeakMap<Page, LibraryFixtureState>();

function libraryLoadPlan() {
  return {
    alias: "qwen-chat", allowed: true, installation_id: "installation-chat", mapping_generation: 4, mapping_id: "mapping-chat",
    nodes: [
      {active_reserved_bytes: 4 * GIB, allowed: true, available_memory_bytes: 100 * GIB, blockers: [], endpoint_owner: true, fabric_address: "fabric://node-alpha", fabric_bandwidth_mbps: 25_000, free_after_bytes: 36 * GIB, inventory_observed_at: "2026-08-15T11:59:50Z", memory_floor_bytes: 8 * GIB, memory_kind: "unified", node_id: "node-alpha", port: 8000, rank: 0, rendezvous_port: 29500, required_memory_bytes: 60 * GIB, role: "leader", warnings: [{code: "run.coexistence_confirmed", detail: "Authoritative capacity evidence permits Qwen Code to coexist."}]},
      {active_reserved_bytes: 4 * GIB, allowed: true, available_memory_bytes: 100 * GIB, blockers: [], endpoint_owner: false, fabric_address: "fabric://node-beta", fabric_bandwidth_mbps: 25_000, free_after_bytes: 36 * GIB, inventory_observed_at: "2026-08-15T11:59:45Z", memory_floor_bytes: 8 * GIB, memory_kind: "unified", node_id: "node-beta", port: 8000, rank: 1, rendezvous_port: null, required_memory_bytes: 60 * GIB, role: "worker", warnings: []},
    ],
    plan_digest: "load-plan-digest", recipe_revision_id: "revision-chat",
  };
}

function libraryOperation(state: string) {
  return {id: "operation-load", kind: "run", owner_id: "installation-chat", state, plan_digest: "load-plan-digest", nodes: ["node-alpha", "node-beta"], result: {job_id: "job-load"}};
}

function telemetry(observedAt: string, sequence = 4, telemetryNodeId = nodeId) {
  return {
    id: `00000000-0000-4000-8000-${String(sequence).padStart(12, "0")}`,
    node_id: telemetryNodeId,
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
        installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", profile_name: "pair", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, borealisId], rank: 0, role: "leader", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
      }],
      loaded: [{
        run_id: "run-chat", installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", alias: "chat", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, borealisId], rank: 0, role: "leader", rank_state: "running", rank_age_seconds: 1, rank_fresh: true, run_state: "running", route_state: "failed", group_state: "degraded", healthy: false, degraded_reason: "route-not-published",
      }, {
        run_id: "run-aurora", installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Aurora solo", alias: "fast-chat", expected_rank_count: 1, present_ranks: [0], member_node_ids: [nodeId], rank: 0, role: "primary", rank_state: "running", rank_age_seconds: 1, rank_fresh: true, run_state: "running", route_state: "published", group_state: "healthy", healthy: true, degraded_reason: null,
      }],
      reservations: {disk_bytes: 2 * GIB, unified_memory_bytes: 4 * GIB, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 1},
      warnings: [{code: "run.degraded", detail: "The Qwen pair route is not published.", severity: "warning"}],
    }, {
      id: borealisId,
      display_name: "Borealis",
      hostname: "borealis.fixture.invalid",
      lifecycle: "managed",
      labels: {role: "inference"},
      connection: {agent_state: "inactive", certificate_state: "expired", online_state: "offline", offline_reason: "certificate-expired", last_seen_at: null, last_seen_age_seconds: null},
      inventory: null,
      telemetry: null,
      installed: [{
        installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", profile_name: "pair", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, borealisId], rank: 1, role: "worker", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
      }],
      loaded: [{
        run_id: "run-chat", installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", alias: "chat", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, borealisId], rank: 1, role: "worker", rank_state: "lost", rank_age_seconds: null, rank_fresh: false, run_state: "lost", route_state: "failed", group_state: "degraded", healthy: false, degraded_reason: "member-rank-unhealthy",
      }],
      reservations: {disk_bytes: 0, unified_memory_bytes: 0, host_memory_bytes: 0, gpu_memory_bytes: 0, port_count: 0},
      warnings: [{code: "node.offline", detail: "Certificate renewal is required.", severity: "error"}, {code: "telemetry.missing", detail: "No telemetry sample is available.", severity: "warning"}, {code: "run.degraded", detail: "The Qwen pair has an unhealthy member rank.", severity: "warning"}],
    }],
  };
}

async function installLocalFleetFixture(page: Page) {
  const snapshot = localSnapshot();
  const libraryState: LibraryFixtureState = {detailFailuresRemaining: 0, empty: false, retryCount: 0, snapshotFailuresRemaining: 0};
  libraryFixtures.set(page, libraryState);
  const platformVersion = `6.0.0-${"release".repeat(24)}`;
  const targetSha256 = "f".repeat(64);
  await page.route("**/api/v1/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
  await page.route("**/api/v1/updates/skew", route => route.fulfill({json: {
    affected_nodes: [nodeId, borealisId],
    digest: `sha256:${"d".repeat(64)}`,
    incompatible_nodes: [],
    nodes: [{active_routes: ["chat"], active_slot: "A", active_workloads: ["run-chat"], build_digest: `sha256:${"a".repeat(64)}`, compatible: true, display_name: `Aurora-${"A".repeat(120)}`, node_id: nodeId, platform_version: "6.0.0", protocol_version: 1, reasons: ["build digest differs"], rollback_slot: "B", status: "online", update_required: true}, {active_routes: [], active_slot: "B", active_workloads: [], build_digest: `sha256:${"b".repeat(64)}`, compatible: true, display_name: `Borealis-${"B".repeat(120)}`, node_id: borealisId, platform_version: "5.9.0", protocol_version: 1, reasons: ["offline pending"], rollback_slot: "A", status: "offline", update_required: true}],
    offline_pending: [borealisId],
    prompt_required: true,
    target: {build_digest: `sha256:${"c".repeat(64)}`, platform_version: platformVersion, protocol_maximum: 1, protocol_minimum: 1, release: `platform/releases/${platformVersion}/${targetSha256}.json`, release_digest: `sha256:${targetSha256}`, target_sha256: targetSha256, tuf_targets_version: 9},
  }}));
  await page.route("**/api/v1/fleet/stream", route => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    headers: {"Cache-Control": "no-cache"},
    body: `retry: 60000\nid: ${snapshot.event_cursor}\nevent: fleet-snapshot\ndata: ${JSON.stringify({schema_version: 1, reset_reason: "initial", snapshot})}\n\n`,
  }));
  await page.route("**/api/v1/fleet", route => route.fulfill({json: snapshot}));
  await page.route("**/api/v1/library?*", route => {
    if (libraryState.snapshotFailuresRemaining > 0) {
      libraryState.snapshotFailuresRemaining -= 1;
      return route.fulfill({status: 200, contentType: "application/json", body: "{"});
    }
    const body = libraryState.empty ? {...librarySnapshot, models: [], unlinked_recipes: []} : librarySnapshot;
    return route.fulfill({json: body});
  });
  await page.route("**/api/v1/library/recipes/recipe-chat", route => {
    if (libraryState.detailFailuresRemaining > 0) {
      libraryState.detailFailuresRemaining -= 1;
      return route.fulfill({status: 200, contentType: "application/json", body: "{"});
    }
    return route.fulfill({json: fullLibraryDetail});
  });
  await page.route("**/api/v1/library/recipes/recipe-unlinked", route => route.fulfill({json: {
    ...minimalLibraryDetail,
    recipe: {
      recipe_id: unlinkedRecipe.recipe_id,
      slug: unlinkedRecipe.slug,
      title: unlinkedRecipe.title,
      description: unlinkedRecipe.description,
      source_kind: unlinkedRecipe.source_kind,
    },
  }}));
  await page.route("**/api/v1/recipes/run-plans/preview", route => route.fulfill({json: libraryLoadPlan()}));
  await page.route("**/api/v1/recipes/runs", async route => {
    libraryState.lastApplyBody = await route.request().postDataJSON() as Record<string, unknown>;
    return route.fulfill({json: libraryOperation("queued")});
  });
  await page.route("**/api/v1/recipes/operations/operation-load", route => route.fulfill({json: libraryOperation("partial")}));
  await page.route("**/api/v1/recipes/operations/operation-load/retry", route => {
    libraryState.retryCount += 1;
    return route.fulfill({json: libraryOperation("queued")});
  });
  await page.route("**/api/v1/jobs/job-load*", route => route.fulfill({json: {
    id: "job-load", kind: "run", state: "failed", base_commit: "", current_attempt: 1,
    operation_total: 2, operations: [], progress: {completed: 1, failed: 1, running: 0, total: 2},
    target_total: 2, targets: ["node-alpha", "node-beta"],
  }}));
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
  await expect(aurora.getByRole("region", {name: "Loaded recipes on Aurora"})).toContainText("Aurora solo");
  await expect(aurora.getByRole("region", {name: "Run state on Aurora"})).toContainText("Degraded · 2 of 2 ranks");
  await expect(aurora.getByRole("region", {name: "Installed recipes on Aurora"})).toContainText("Complete · 2 of 2 ranks");
  const borealis = page.getByRole("article", {name: "Borealis — Offline"});
  await expect(borealis).toContainText("Certificate expired");
  await expect(borealis.getByRole("region", {name: "Installed recipes on Borealis"})).toContainText("worker rank 1");
  await expect(borealis.getByRole("region", {name: "Run state on Borealis"})).toContainText("member rank unhealthy");

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
  const updateNotice = page.getByRole("region", {name: "GPU node update available"});
  await expect(updateNotice).toBeVisible();
  await expect(updateNotice).toContainText(`sha256:${"c".repeat(64)}`);
  await expect(updateNotice).toContainText(nodeId);
  await page.getByRole("button", {name: "View Aurora details"}).click();

  for (const width of [360, 768, 1280, 1920]) {
    await page.setViewportSize({width, height: width === 360 ? 800 : 900});
    await expect.poll(() => page.evaluate(() => ({
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      noticeFits: (() => {
        const notice = document.querySelector<HTMLElement>(".update-notice");
        return notice ? notice.scrollWidth <= notice.clientWidth : false;
      })(),
      viewport: window.innerWidth,
    }))).toEqual({body: width, document: width, noticeFits: true, viewport: width});
  }

  await page.setViewportSize({width: 360, height: 800});
  await expect(page.locator(".node-detail")).toHaveCSS("position", "static");
  await page.setViewportSize({width: 1920, height: 900});
  await expect(page.locator(".node-detail")).toHaveCSS("position", "sticky");
  await page.getByRole("button", {name: "Close Aurora details"}).click();
  const columns = await page.locator(".node-grid").evaluate(element => getComputedStyle(element).gridTemplateColumns.split(" ").length);
  expect(columns).toBeGreaterThanOrEqual(2);
});

test("Library keeps URL drill-down below 900px and three coordinated panes above it", async ({page}, testInfo) => {
  await page.setViewportSize({width: 360, height: 800});
  await page.goto("/library");

  const models = page.getByRole("region", {name: "Models"});
  const recipes = page.getByRole("region", {name: "Recipes"});
  const detail = page.getByRole("region", {name: "Recipe detail"});
  await expect(models).toBeVisible();
  await expect(recipes).toBeHidden();
  await expect(detail).toBeHidden();

  await models.getByRole("link", {name: /Qwen 3/}).focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/library\/models\/qwen%2F3$/);
  await expect(page.getByRole("heading", {name: "Library", exact: true})).toBeFocused();
  await expect(models).toBeHidden();
  await expect(page.getByRole("region", {name: "Recipes for Qwen 3"})).toBeVisible();
  await expect(detail).toBeHidden();

  await page.getByRole("link", {name: /Qwen Chat/}).click();
  await expect(page).toHaveURL(/\/library\/recipes\/recipe-chat$/);
  await expect(page.getByRole("heading", {name: "Library", exact: true})).toBeFocused();
  await expect(models).toBeHidden();
  await expect(page.getByRole("region", {name: "Recipes for Qwen 3"})).toBeHidden();
  await expect(detail).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL(/\/library\/models\/qwen%2F3$/);
  await expect(page.getByRole("region", {name: "Recipes for Qwen 3"})).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/library$/);
  await expect(models).toBeVisible();

  await models.getByRole("link", {name: /Unlinked/}).click();
  await expect(page).toHaveURL(/\/library\/models\/~unlinked$/);
  const unlinked = page.getByRole("region", {name: "Unlinked recipes"});
  await unlinked.getByRole("link", {name: /Custom Runtime/}).click();
  await expect(page).toHaveURL(/\/library\/recipes\/recipe-unlinked$/);
  const backToUnlinked = page.getByRole("link", {name: "Back to Unlinked recipes"});
  await expect(backToUnlinked).toHaveAttribute("href", "/library/models/~unlinked");
  await backToUnlinked.click();
  await expect(page).toHaveURL(/\/library\/models\/~unlinked$/);
  await expect(unlinked).toBeVisible();

  await page.setViewportSize({width: 1280, height: 900});
  await models.getByRole("link", {name: /Qwen 3/}).click();
  await page.getByRole("link", {name: /Qwen Chat/}).click();
  await expect(models).toBeVisible();
  await expect(page.getByRole("region", {name: "Recipes for Qwen 3"})).toBeVisible();
  await expect(detail).toBeVisible();

  for (const width of [320, 360, 768, 899, 900, 1280, 1920]) {
    await page.setViewportSize({width, height: width < 900 ? 800 : 900});
    await expect.poll(() => page.evaluate(() => ({
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))).toEqual({body: width, document: width, viewport: width});
  }

  await page.setViewportSize({width: 899, height: 900});
  await expect(models).toBeHidden();
  await expect(detail).toBeVisible();
  await page.setViewportSize({width: 900, height: 900});
  await expect(models).toBeVisible();
  await expect(page.getByRole("region", {name: "Recipes for Qwen 3"})).toBeVisible();
  await expect(detail).toBeVisible();
  await page.setViewportSize({width: 360, height: 800});
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({path: testInfo.outputPath("library-mobile.png")});
});

test("Library fixture journey keeps visual authority primary through preview, partial retry, and Advanced recovery", async ({page}, testInfo) => {
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto("/library/recipes/recipe-chat");

  const models = page.getByRole("region", {name: "Models"});
  const recipes = page.getByRole("region", {name: "Recipes for Qwen 3"});
  const authority = page.getByRole("region", {name: "Qwen Chat recipe authority"});
  await expect(models.getByRole("link", {name: /Unlinked/})).toBeVisible();
  await expect(recipes.getByRole("link", {name: /Qwen Chat/})).toBeVisible();
  await expect(recipes.getByRole("link", {name: /Qwen Code/})).toBeVisible();
  await expect(authority).toContainText("Immutable revision 3");
  await expect(authority).toContainText("Bounded search is incomplete");
  await expect(authority).toContainText("Inventory fresh · 10s");
  await expect(authority).toContainText("node-alpha + node-beta");
  const authorityContrast = await authority.evaluate(element => {
    const channels = (value: string) => value.match(/[\d.]+/g)!.slice(0, 3).map(Number);
    const luminance = (value: string) => {
      const linear = channels(value).map(channel => {
        const normalized = channel / 255;
        return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
    };
    const foreground = luminance(getComputedStyle(element).color);
    const pane = element.closest<HTMLElement>(".library-pane")!;
    const background = luminance(getComputedStyle(pane).backgroundColor);
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
  });
  expect(authorityContrast).toBeGreaterThanOrEqual(4.5);

  const selector = authority.getByRole("button", {name: "Select complete group node-alpha and node-beta"});
  await selector.focus();
  await page.keyboard.press("Space");
  await expect(selector).toHaveAttribute("aria-pressed", "true");
  const review = authority.getByRole("button", {name: "Review Load"});
  await review.click();
  let dialog = page.getByRole("dialog", {name: "Review Load"});
  await expect(dialog).toContainText("Existing recipes remain loaded. Forge will not unload anything automatically.");
  await expect(dialog).toContainText("Authoritative capacity evidence permits Qwen Code to coexist.");
  await dialog.getByRole("button", {name: "Cancel"}).click();
  await expect(dialog).toBeHidden();
  await expect(review).toBeFocused();

  await review.click();
  dialog = page.getByRole("dialog", {name: "Review Load"});
  await dialog.getByRole("button", {name: "Load selected installation"}).click();
  const progress = page.getByRole("region", {name: "Load operation progress"});
  await expect(progress).toContainText("Operation incomplete");
  await expect(progress).toContainText("1 of 2 ranks completed · 1 failed");
  const state = libraryFixtures.get(page)!;
  expect(state.lastApplyBody).toMatchObject({alias: "qwen-chat", installation_id: "installation-chat", plan_digest: "load-plan-digest"});
  await progress.getByRole("button", {name: "Retry incomplete operation"}).click();
  await expect.poll(() => state.retryCount).toBe(1);
  await expect(progress).toContainText("Operation incomplete");

  const advanced = page.getByRole("group", {name: "Advanced recipe document"});
  await advanced.getByText("Advanced recipe document").click();
  const editor = advanced.getByRole("textbox", {name: "Recipe JSON"});
  const firstValid = {...fullLibraryDetail.visual_recipe!, workload: {...fullLibraryDetail.visual_recipe!.workload, family: "qwen/e2e"}};
  await editor.fill(JSON.stringify(firstValid, null, 2));
  await expect(authority.getByRole("region", {name: "Model and runtime"})).toContainText("qwen/e2e");
  const invalid = {...firstValid, workload: {...firstValid.workload, family: 4}};
  await editor.fill(JSON.stringify(invalid, null, 2));
  await expect(advanced.getByRole("alert")).toContainText("$.workload.family must be a string.");
  await expect(editor).toBeFocused();
  await expect(authority.getByRole("region", {name: "Model and runtime"})).toContainText("qwen/e2e");

  const upload = advanced.getByLabel("Upload recipe JSON");
  const uploaded = {...firstValid, workload: {...firstValid.workload, family: "qwen/uploaded"}};
  await upload.focus();
  await upload.setInputFiles({name: "recipe.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(uploaded))});
  await expect(advanced.getByRole("alert")).toHaveCount(0);
  await expect(upload).toBeFocused();
  await expect(authority.getByRole("region", {name: "Model and runtime"})).toContainText("qwen/uploaded");
  await expect(page.getByRole("link", {name: "Source and build"})).toBeVisible();
  await expect(page.getByRole("link", {name: "Cluster mapping"})).toBeVisible();
  await expect(page.getByRole("link", {name: "Raw editor"})).toBeVisible();

  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({path: testInfo.outputPath("library-desktop.png")});
});

test("Library local fixture recovers from errors and exposes an empty-state escape hatch", async ({page}) => {
  const state = libraryFixtures.get(page)!;
  state.snapshotFailuresRemaining = 1;
  await page.goto("/library");
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("button", {name: "Retry Library"}).click();
  await expect(page.getByRole("region", {name: "Models"})).toBeVisible();

  state.detailFailuresRemaining = 1;
  await page.getByRole("link", {name: /Qwen 3/}).click();
  await page.getByRole("link", {name: /Qwen Chat/}).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("button", {name: "Retry recipe detail"}).click();
  await expect(page.getByRole("region", {name: "Qwen Chat recipe authority"})).toBeVisible();

  state.empty = true;
  await page.goto("/library");
  await expect(page.getByRole("heading", {name: "No recipes in the Library"})).toBeVisible();
  await expect(page.getByRole("link", {name: "Open advanced catalog"})).toHaveAttribute("href", "/catalog");
});
