import AxeBuilder from "@axe-core/playwright";
import {expect, test, type Page} from "@playwright/test";
import {codeRecipe, fullLibraryDetail, librarySnapshot, minimalLibraryDetail, unlinkedRecipe} from "../src/test-fixtures/library";

const GIB = 1024 ** 3;
const nodeId = "spk_0123456789abcdef0123456789abcdef";
const borealisId = "spk_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const commit = "a".repeat(40);
const qwenModel = `qwen/3@${"e".repeat(64)}`;
const qwenModelPath = `/library/models/${encodeURIComponent(qwenModel)}`;
const qwenModelName = "Qwen 3";
const browserProblems = new WeakMap<Page, string[]>();
type LibraryFixtureState = {
  detailFailuresRemaining: number;
  empty: boolean;
  lastApplyBody?: Record<string, unknown>;
  retryCount: number;
  snapshotFailuresRemaining: number;
};
const libraryFixtures = new WeakMap<Page, LibraryFixtureState>();

function libraryCatalogUpdate() {
  const contentDigest = "b".repeat(64);
  return {
    publisher: "vonk-forge", slug: "qwen-chat", title: "Qwen Chat catalog recipe", description: "A digest-bound Qwen Chat recipe.", tags: ["qwen", "chat"],
    uri: `vonk://catalog/vonk-forge/qwen-chat@sha256:${contentDigest}`, content_sha256: contentDigest,
    model_publisher: "qwen", model_slug: "3", model_title: "Qwen 3", source_owner: "QwenLM", source_repository: "https://github.com/QwenLM/Qwen3",
    capabilities: ["chat"], qualification: "cataloged", qualification_basis: "explicit-accepted-metadata", qualification_detail: "The reviewed immutable recipe explicitly declares accepted qualification.",
    execution_readiness: "executable", execution_readiness_basis: "explicit-executable-metadata", execution_readiness_detail: "This recipe explicitly declares an executable contract; fleet compatibility and operator review still apply.",
    precision: "BF16", execution_harness: "vllm-openai", runtime_distribution: "vllm-0-27-1", source_bundle_sha256: "9".repeat(64), artifact_count: 1,
    topology_name: "pair", topology_mode: "tensor_parallel", node_count: 2, topology_roles: [{name: "entrypoint", count: 1, endpoint_owner: true}, {name: "worker", count: 1, endpoint_owner: false}],
    fabric: {connectivity: "connected", minimum_bandwidth_mbps: 25_000}, expected_download_bytes: 80 * GIB, maximum_installed_bytes_per_node: 100 * GIB, maximum_runtime_memory_bytes_per_node: 72 * GIB,
    release_version: "1.2.0", release_released_at: "2026-08-24",
    local: {status: "update-available", recipe_id: "recipe-chat", revision_number: 3, content_sha256: "a".repeat(64), release_version: "1.0.0"},
  };
}

async function expectNoSeriousAccessibilityViolations(page: Page) {
  const results = await new AxeBuilder({page}).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations, results.violations.map(value => `${value.id}: ${value.help}`).join("\n")).toEqual([]);
}

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
    authority_revision: commit,
    nodes: [{
      id: nodeId,
      display_name: nodeId,
      hostname: "aurora.fixture.invalid",
      lifecycle: "managed",
      labels: {role: "inference"},
      connection: {agent_state: "active", certificate_state: "valid", online_state: "online", offline_reason: null, last_seen_at: observedAt, last_seen_age_seconds: 0},
      inventory: null,
      telemetry: {age_seconds: 0, freshness: "live", sample: telemetry(observedAt)},
      installed: [{
        installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", topology_name: "pair", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, borealisId], rank: 0, role: "leader", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
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
      display_name: borealisId,
      hostname: "borealis.fixture.invalid",
      lifecycle: "managed",
      labels: {role: "inference"},
      connection: {agent_state: "inactive", certificate_state: "expired", online_state: "offline", offline_reason: "certificate-expired", last_seen_at: null, last_seen_age_seconds: null},
      inventory: null,
      telemetry: null,
      installed: [{
        installation_id: "install-chat", recipe_id: "recipe-chat", recipe_revision_id: "revision-chat", title: "Qwen pair", topology_name: "pair", expected_rank_count: 2, present_ranks: [0, 1], member_node_ids: [nodeId, borealisId], rank: 1, role: "worker", rank_state: "installed", group_state: "installed", complete: true, degraded_reason: null,
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
  await page.route("**/api/v1/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
  await page.route("**/api/v1/fleet/stream", route => route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    headers: {"Cache-Control": "no-cache"},
    body: `retry: 60000\nid: ${snapshot.event_cursor}\nevent: fleet-snapshot\ndata: ${JSON.stringify({schema_version: 1, reset_reason: "initial", snapshot})}\n\n`,
  }));
  await page.route("**/api/v1/fleet", route => route.fulfill({json: snapshot}));
  await page.route("**/api/v1/catalog/public-recipes", route => route.fulfill({json: {repository: "CarstVaartjes/vonk-forge-recipes", commit, recipes: []}}));
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
  await page.route("**/api/v1/library/recipes/recipe-code", route => route.fulfill({json: {
    ...fullLibraryDetail,
    recipe: {...fullLibraryDetail.recipe, recipe_id: codeRecipe.recipe_id, slug: codeRecipe.slug, title: codeRecipe.title, description: codeRecipe.description},
  }}));
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
    id: "job-load", kind: "run", state: "failed", authority_revision: commit, current_attempt: 1,
    operation_total: 2, operations: [], progress: {completed: 1, failed: 1, running: 0, total: 2},
    target_total: 2, targets: ["node-alpha", "node-beta"],
  }}));
  await page.route("**/api/v1/nodes/*/telemetry?*", route => {
    const url = new URL(route.request().url());
    const start = url.searchParams.get("start") ?? snapshot.generated_at;
    const end = url.searchParams.get("end") ?? snapshot.generated_at;
    const resolution = url.searchParams.get("resolution") ?? "raw";
    const maximumPoints = Number(url.searchParams.get("maximum_points") ?? 360);
    const first = telemetry(start, 1);
    const last = {...telemetry(end, 2), gpu_utilization_percent: 72, temperature_c: 45};
    const points = resolution === "raw" ? [first, last] : [{
      node_id: nodeId,
      resolution,
      bucket_start: start,
      bucket_end: end,
      source_sample_count: 2,
      gap_samples: 0,
      metrics: {
        gpu_utilization_percent: {count: 2, minimum: 61, mean: 66.5, maximum: 72},
        memory_available_bytes: {count: 2, minimum: 90 * GIB, mean: 91 * GIB, maximum: 92 * GIB},
        temperature_c: {count: 2, minimum: 43.5, mean: 44.25, maximum: 45},
      },
    }];
    return route.fulfill({json: {schema_version: 1, node_id: nodeId, start, end, resolution, maximum_points: maximumPoints, points}});
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

test("Fleet Detailed view and bounded history are keyboard-accessible with local evidence", async ({page}) => {
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
  await expect(page.getByRole("img", {name: "Aurora GPU utilization history"})).toHaveAccessibleDescription(/1 reported buckets/);
  await expectNoSeriousAccessibilityViolations(page);
  await page.getByRole("button", {name: "24 hours"}).click();
  await expect(page.getByRole("button", {name: "24 hours"})).toHaveAttribute("aria-pressed", "true");
});

test("Fleet discovery searches friendly names and combines actionable health filters", async ({page}) => {
  await page.goto("/fleet");
  await expect(page.getByRole("button", {name: "Detailed"})).toHaveAttribute("aria-pressed", "true");

  const search = page.getByRole("searchbox", {name: "Find a Spark"});
  await search.fill("Borealis");
  await expect(page.getByRole("article", {name: "Borealis — Offline"})).toBeVisible();
  await expect(page.getByRole("article", {name: /Aurora —/})).toHaveCount(0);
  await expect(page.getByRole("status").filter({hasText: "Showing 1 of 2 Sparks"})).toBeVisible();
  await page.getByRole("button", {name: "Clear filters"}).click();

  await page.getByRole("button", {name: "Show offline nodes"}).click();
  await expect(page.getByRole("checkbox", {name: "Offline 1"})).toBeChecked();
  await page.getByRole("checkbox", {name: /Live 1/}).check();
  await expect(page.getByRole("status").filter({hasText: "Showing 2 of 2 Sparks"})).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("Node history chooses honest rollups on desktop and mobile", async ({page}) => {
  for (const width of [1280, 360]) {
    await page.setViewportSize({width, height: width === 360 ? 800 : 900});
    await page.goto("/fleet");
    await page.getByRole("button", {name: "View Aurora details"}).click();
    await expect(page.getByRole("button", {name: "Close Aurora details"})).toBeFocused();

    await page.getByRole("button", {name: "7 days"}).click();
    await expect(page.getByRole("button", {name: "7 days"})).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByText(/Showing 15-minute buckets across the full 7-day window/)).toBeVisible();
    await expect(page.getByRole("img", {name: "Aurora GPU utilization history"})).toHaveAccessibleDescription(/reported buckets/);

    await page.getByRole("button", {name: "1 year"}).click();
    await expect(page.getByText(/Showing newest 1,500 15-minute buckets within 1 year/)).toBeVisible();
    await expect.poll(() => page.evaluate(() => ({
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))).toEqual({body: width, document: width, viewport: width});
  }
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

test("Fleet compact and topology views persist, reflow, and keep technical IDs out of browse views", async ({page}, testInfo) => {
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto("/fleet");
  await page.getByRole("button", {name: "Topology"}).click();

  await expect(page.getByRole("region", {name: "Fleet topology"})).toBeVisible();
  await expect(page.getByRole("button", {name: /View Aurora details/})).toBeVisible();
  await expect(page.getByText("Qwen pair", {exact: true}).first()).toBeVisible();
  await expect(page.getByText(nodeId, {exact: true})).toHaveCount(0);
  await expectNoSeriousAccessibilityViolations(page);
  await page.screenshot({path: testInfo.outputPath("fleet-topology-desktop.png"), fullPage: true});

  await page.reload();
  await expect(page.getByRole("button", {name: "Topology"})).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", {name: "Compact"}).click();
  await expect(page.getByRole("region", {name: "Fleet nodes compact table"})).toBeVisible();

  for (const width of [320, 360, 760, 1280]) {
    await page.setViewportSize({width, height: width <= 360 ? 800 : 900});
    await expect.poll(() => page.evaluate(() => ({
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))).toEqual({body: width, document: width, viewport: width});
  }
  await page.setViewportSize({width: 360, height: 800});
  await expectNoSeriousAccessibilityViolations(page);
  await page.screenshot({path: testInfo.outputPath("fleet-compact-mobile.png"), fullPage: true});
});

test("Add Spark preserves an in-flight and revealed one-time grant until an explicit decision", async ({page}) => {
  let releaseGrant!: () => void;
  const grantGate = new Promise<void>(resolve => { releaseGrant = resolve; });
  let grantRequests = 0;
  await page.route("**/api/v1/agents/enrollments/grants", async route => {
    grantRequests += 1;
    await grantGate;
    await route.fulfill({status: 201, json: {
      id: "grant-e2e", purpose: "new-node", token: "short-lived-e2e-secret", expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
      controller_endpoint: "https://controller.fixture.invalid:9443",
      enrollment_endpoint: "https://enrollment.fixture.invalid:9444",
      ca_fingerprint: "d".repeat(64),
      controller_address: "192.168.1.231",
      service_hostnames: ["controller.fixture.invalid", "enrollment.fixture.invalid"],
    }});
  });
  await page.goto("/fleet");
  await page.getByRole("button", {name: "Add Spark"}).click();
  const dialog = page.getByRole("dialog", {name: "Add Spark"});
  await dialog.getByRole("button", {name: "Create one-time enrollment command"}).click();
  await expect.poll(() => grantRequests).toBe(1);

  await expect(dialog).toHaveAttribute("aria-busy", "true");
  await expect(dialog.getByRole("button", {name: "Close Add Spark"})).toBeDisabled();
  await expect(dialog.getByRole("button", {name: "Cancel"})).toBeDisabled();
  await page.keyboard.press("Escape");
  await page.locator(".library-dialog-backdrop").evaluate(element => element.dispatchEvent(new MouseEvent("mousedown", {bubbles: true})));
  await expect(dialog).toBeVisible();
  await page.getByRole("link", {name: "Library"}).dispatchEvent("click");
  await expect(page).toHaveURL(/\/fleet$/);

  releaseGrant();
  await expect(dialog.getByText("short-lived-e2e-secret")).toBeVisible();
  await expect(dialog).not.toHaveAttribute("aria-busy");
  await dialog.getByRole("button", {name: "Close Add Spark"}).click();
  await expect(dialog.getByText("Discard this one-time grant?")).toBeVisible();
  await expect(dialog.getByRole("button", {name: "Keep grant open"})).toBeFocused();
  await expectNoSeriousAccessibilityViolations(page);
  await dialog.getByRole("button", {name: "Keep grant open"}).click();
  await expect(dialog.getByText("Discard this one-time grant?")).toBeHidden();
  await dialog.getByRole("button", {name: "I saved these values — Done"}).click();
  await expect(dialog).toBeHidden();
  await page.getByRole("link", {name: "Library"}).click();
  await expect(page).toHaveURL(/\/library$/);
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

  await models.getByRole("link", {name: new RegExp(qwenModelName)}).focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`${qwenModelPath}$`));
  await expect(page.getByRole("heading", {name: "Library", exact: true})).toBeFocused();
  await expect(models).toBeHidden();
  await expect(page.getByRole("region", {name: `Recipes for ${qwenModelName}`})).toBeVisible();
  await expect(detail).toBeHidden();

  await page.getByRole("link", {name: /Qwen Chat/}).click();
  await expect(page).toHaveURL(/\/library\/recipes\/recipe-chat$/);
  await expect(page.getByRole("heading", {name: "Library", exact: true})).toBeFocused();
  await expect(models).toBeHidden();
  await expect(page.getByRole("region", {name: `Recipes for ${qwenModelName}`})).toBeHidden();
  await expect(detail).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`${qwenModelPath}$`));
  await expect(page.getByRole("region", {name: `Recipes for ${qwenModelName}`})).toBeVisible();
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
  await models.getByRole("link", {name: new RegExp(qwenModelName)}).click();
  await page.getByRole("link", {name: /Qwen Chat/}).click();
  await expect(models).toBeVisible();
  await expect(page.getByRole("region", {name: `Recipes for ${qwenModelName}`})).toBeVisible();
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
  await expect(page.getByRole("region", {name: `Recipes for ${qwenModelName}`})).toBeVisible();
  await expect(detail).toBeVisible();

  await page.setViewportSize({width: 1280, height: 900});
  await page.evaluate(() => {
    const frame = document.createElement("iframe");
    frame.title = "Fractional Library viewport";
    frame.style.width = "899.5px";
    frame.style.height = "800px";
    frame.src = "/library/recipes/recipe-chat";
    document.body.append(frame);
  });
  const fractionalFrame = page.frameLocator('iframe[title="Fractional Library viewport"]');
  await expect.poll(() => page.locator('iframe[title="Fractional Library viewport"]').evaluate(element => element.getBoundingClientRect().width)).toBe(899.5);
  await fractionalFrame.locator(".library-browser").waitFor();
  await fractionalFrame.locator("html").evaluate(() => {
    for (const sheet of Array.from(document.styleSheets)) {
      for (const rule of Array.from(sheet.cssRules)) {
        if (rule instanceof CSSMediaRule && /(?:899|900)px/.test(rule.conditionText)) rule.media.mediaText = "not all";
      }
    }
  });
  await expect(fractionalFrame.getByRole("region", {name: "Models"})).toBeHidden();
  await expect(fractionalFrame.getByRole("region", {name: `Recipes for ${qwenModelName}`})).toBeHidden();
  await expect(fractionalFrame.getByRole("region", {name: "Recipe detail"})).toBeVisible();
  await page.locator('iframe[title="Fractional Library viewport"]').evaluate(element => element.remove());

  await page.setViewportSize({width: 360, height: 800});
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({path: testInfo.outputPath("library-mobile.png")});
});

test("Library view modes persist and compare friendly recipes without document overflow", async ({page}) => {
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto("/library");

  const models = page.getByRole("region", {name: "Models"});
  const modelLink = models.getByRole("link", {name: /Qwen 3/});
  await expect(modelLink).not.toContainText(/qwen\/3@/);
  const modelRow = modelLink.locator("xpath=ancestor::article");
  await modelRow.getByText("Technical details").click();
  await expect(modelRow).toContainText("e".repeat(64));
  await expect(modelRow.getByRole("button", {name: "Copy Model digest"})).toBeVisible();

  await page.getByRole("button", {name: "Compact"}).click();
  await expect(page.getByRole("region", {name: "Compact recipe list"})).toBeVisible();
  await expect.poll(() => page.evaluate(() => localStorage.getItem("vonk-forge.library.view-mode"))).toBe("compact");
  await page.reload();
  await expect(page.getByRole("button", {name: "Compact"})).toHaveAttribute("aria-pressed", "true");

  await page.getByRole("button", {name: "Compare"}).click();
  const picker = page.getByRole("region", {name: "Choose recipes to compare"});
  await picker.getByRole("checkbox", {name: /Qwen Chat/}).check();
  await picker.getByRole("checkbox", {name: /Qwen Code/}).check();
  const comparison = page.getByRole("region", {name: "Recipe comparison"});
  await expect(comparison.getByLabel("Startup memory: 144.0 GiB")).toHaveCount(2);
  await expect(comparison.getByText("2 Sparks")).toHaveCount(2);
  await expect(comparison.getByText("Ready")).toHaveCount(2);

  for (const width of [320, 360, 768, 1280]) {
    await page.setViewportSize({width, height: width < 768 ? 800 : 900});
    await expect.poll(() => page.evaluate(() => ({
      body: document.body.scrollWidth,
      document: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))).toEqual({body: width, document: width, viewport: width});
  }

  await page.setViewportSize({width: 360, height: 800});
  await page.goto("/library");
  const kpiLayout = await page.locator(".library-overview").evaluate(element => ({
    horizontallyScrollable: element.scrollWidth > element.clientWidth,
    rows: new Set(Array.from(element.children).map(child => (child as HTMLElement).offsetTop)).size,
  }));
  expect(kpiLayout).toEqual({horizontallyScrollable: true, rows: 1});
});

test("Library exposes a versioned catalog update and opens its changelog review", async ({page}) => {
  const update = libraryCatalogUpdate();
  await page.unroute("**/api/v1/catalog/public-recipes");
  await page.route("**/api/v1/catalog/public-recipes", route => route.fulfill({json: {repository: "CarstVaartjes/vonk-forge-recipes", commit, recipes: [update]}}));
  await page.route("**/api/v1/catalog/imports/public/preview", route => route.fulfill({json: {
    ...update,
    source: "recipe_library",
    changes_since_local: [{
      version: "1.2.0", released_at: "2026-08-24", content_sha256: update.content_sha256, upgrade_effect: "rebuild",
      changes: [{kind: "performance", summary: "Improved distributed defaults.", details: "Uses the current upstream topology guidance.", references: ["https://github.com/QwenLM/Qwen3"]}],
    }],
  }}));

  await page.goto("/library");
  await expect(page.getByRole("group", {name: "1 catalog update available"})).toBeVisible();
  await page.getByRole("link", {name: /Qwen 3/}).click();
  const recipes = page.getByRole("region", {name: `Recipes for ${qwenModelName}`});
  await expect(recipes.getByText("Update available · v1.0.0 → v1.2.0")).toBeVisible();
  await recipes.getByRole("link", {name: "Review update for Qwen Chat"}).click();

  await expect(page).toHaveURL(/\/library\/import\?recipe=/);
  const changelog = page.getByRole("region", {name: "Changes since local v1.0.0"});
  await expect(changelog).toContainText("Improved distributed defaults.");
  await expect(changelog).toContainText("Rebuild required");
  await expect(page.getByText(/Existing installations and running services remain pinned/)).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("Library fixture journey keeps visual authority primary through preview, partial retry, and Advanced recovery", async ({page}, testInfo) => {
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto("/library/recipes/recipe-chat");

  const models = page.getByRole("region", {name: "Models"});
  const recipes = page.getByRole("region", {name: `Recipes for ${qwenModelName}`});
  const authority = page.getByRole("region", {name: "Qwen Chat recipe authority"});
  await expect(models.getByRole("link", {name: /Unlinked/})).toBeVisible();
  await expect(recipes.getByRole("link", {name: /Qwen Chat/})).toBeVisible();
  await expect(recipes.getByRole("link", {name: /Qwen Code/})).toBeVisible();
  await expect(authority).toContainText("Immutable revision 3");
  await expect(authority).toContainText("Bounded search is incomplete");
  await expect(authority).toContainText("Inventory fresh · 10s");
  await expect(authority).toContainText("Spark node + Spark node");
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

  const selector = authority.getByRole("button", {name: "Select complete group Spark node and Spark node"});
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
  const firstValid = {...fullLibraryDetail.visual_recipe!, model: {...fullLibraryDetail.visual_recipe!.model, slug: "qwen-e2e"}};
  await editor.fill(JSON.stringify(firstValid, null, 2));
  await expect(authority.getByRole("region", {name: "Recipe identity"})).toContainText("Qwen E 2 E");
  const invalid = {...firstValid, model: {...firstValid.model, content_sha256: "not-a-digest"}};
  await editor.fill(JSON.stringify(invalid, null, 2));
  await expect(advanced.getByRole("alert")).toContainText("$.model.content_sha256 must be 64 lowercase hexadecimal characters.");
  await expect(editor).toBeFocused();
  await expect(authority.getByRole("region", {name: "Recipe identity"})).toContainText("Qwen E 2 E");

  const upload = advanced.getByLabel("Upload recipe JSON");
  const uploaded = {...firstValid, model: {...firstValid.model, slug: "qwen-uploaded"}};
  await upload.focus();
  await upload.setInputFiles({name: "recipe.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(uploaded))});
  await expect(advanced.getByRole("alert")).toHaveCount(0);
  await expect(upload).toBeFocused();
  await expect(authority.getByRole("region", {name: "Recipe identity"})).toContainText("Qwen Uploaded");
  await expect(page.getByRole("link", {name: "Source and build"})).toHaveCount(0);
  await expect(page.getByRole("link", {name: "Cluster mapping"})).toHaveCount(0);
  await expect(page.getByRole("link", {name: "Raw editor"})).toHaveCount(0);

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
  await page.getByRole("link", {name: new RegExp(qwenModelName)}).click();
  await page.getByRole("link", {name: /Qwen Chat/}).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("button", {name: "Retry recipe detail"}).click();
  await expect(page.getByRole("region", {name: "Qwen Chat recipe authority"})).toBeVisible();

  state.empty = true;
  await page.goto("/library");
  await expect(page.getByRole("heading", {name: "Bring your first recipe into the Library"})).toBeVisible();
  await expect(page.getByRole("region", {name: "Empty Library"}).getByRole("link", {name: "Browse public recipes"})).toBeVisible();
  await expect(page.getByRole("link", {name: /advanced/i})).toHaveCount(0);
});
