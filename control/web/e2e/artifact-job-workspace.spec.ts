import AxeBuilder from "@axe-core/playwright";
import {expect, test, type Page} from "@playwright/test";
import {mkdir} from "node:fs/promises";
import {resolve} from "node:path";
import {fullLibraryDetail, librarySnapshot} from "../src/test-fixtures/library";

const runId = "00000000-0000-4000-8000-000000000010";
const reviewDirectory = resolve(import.meta.dirname, "../../../.impeccable/review");
type FixtureMode = "normal" | "empty" | "failed";
const fixtureStates = new WeakMap<Page, {mode: FixtureMode}>();

function artifactDetail() {
  const detail = structuredClone(fullLibraryDetail) as typeof fullLibraryDetail & {visual_recipe: Record<string, unknown> & {interfaces: unknown[]}};
  detail.recipe.title = "Aurora media workcell";
  detail.recipe.description = "A bounded artifact workflow with durable controller evidence.";
  detail.visual_recipe = {
    ...detail.visual_recipe,
    metadata: {title: detail.recipe.title, description: detail.recipe.description, tags: ["artifact", "media"]},
    model_license: null,
    parameters: [{name: "seed", description: "Reproducible generation seed.", type: "integer", default: 42, minimum: 0, maximum: 2_147_483_647, allowed_values: [], pattern: null, change_effect: "restart"}],
    interfaces: [{
      adapter: "artifact-job", path: "/outputs", timeout_seconds: 3600,
      input: {
        path: "/inputs", required: true, media_types: ["text/plain", "image/png"], max_bytes: 64 * 1024 ** 2, min_files: 1, max_files: 2,
        slots: [
          {id: "prompt", label: "Prompt", description: "The production brief saved as UTF-8 text.", media_types: ["text/plain"], extensions: [".txt"], min_files: 1, max_files: 1, max_file_bytes: 16_384, max_total_bytes: 16_384},
          {id: "reference", label: "Reference image", description: "Optional source composition or palette.", media_types: ["image/png"], extensions: [".png"], min_files: 0, max_files: 1, max_file_bytes: 64 * 1024 ** 2, max_total_bytes: 64 * 1024 ** 2},
        ],
      },
      output: {
        path: "/outputs", allowed_media_types: ["image/png", "audio/wav", "model/gltf-binary"], max_total_bytes: 256 * 1024 ** 2,
        slots: [
          {id: "images", label: "Images", description: "Generated stills.", media_types: ["image/png"], extensions: [".png"], min_files: 1, max_files: 2, max_file_bytes: 64 * 1024 ** 2, max_total_bytes: 128 * 1024 ** 2},
          {id: "audio", label: "Audio", description: "Generated soundtrack.", media_types: ["audio/wav"], extensions: [".wav"], min_files: 0, max_files: 1, max_file_bytes: 64 * 1024 ** 2, max_total_bytes: 64 * 1024 ** 2},
          {id: "mesh", label: "Mesh", description: "Generated scene geometry.", media_types: ["model/gltf-binary"], extensions: [".glb"], min_files: 0, max_files: 1, max_file_bytes: 64 * 1024 ** 2, max_total_bytes: 64 * 1024 ** 2},
        ],
      },
    }],
  };
  detail.operational_state.runs = [{
    installation_id: "installation-chat", mapping_id: "mapping-chat", node_ids: ["node-alpha", "node-beta"], recipe_revision_id: "revision-chat",
    route_state: "published", run_id: runId, state: "running",
  }];
  return detail;
}

function artifactJob(id: string, state: "ready" | "running" | "succeeded" | "failed", createdAt: string) {
  const succeeded = state === "succeeded";
  return {
    id, run_id: runId, operation_id: state === "ready" ? null : `operation-${id}`, interface: "artifact-job", state,
    contract_sha256: "8".repeat(64), compiled_contract: {},
    input_manifest_sha256: "a".repeat(64), input_total_bytes: 8_192,
    input_declarations: [{slot: "prompt", name: "prompt.txt", media_type: "text/plain", size_bytes: 8_192, sha256: "b".repeat(64)}],
    input_files: [{slot: "prompt", name: "prompt.txt", media_type: "text/plain", size_bytes: 8_192, sha256: "b".repeat(64)}],
    output_limits: {max_files: 4, max_file_bytes: 64 * 1024 ** 2, max_total_bytes: 256 * 1024 ** 2, allowed_media_types: ["image/png", "audio/wav", "model/gltf-binary"]},
    output_manifest_sha256: succeeded ? "c".repeat(64) : null,
    output_files: succeeded ? [
      {name: "aurora-frame.png", media_type: "image/png", size_bytes: 68, sha256: "d".repeat(64)},
      {name: "aurora-score.wav", media_type: "audio/wav", size_bytes: 44, sha256: "e".repeat(64)},
      {name: "aurora-scene.glb", media_type: "model/gltf-binary", size_bytes: 12_288, sha256: "f".repeat(64)},
    ] : [],
    result_evidence: succeeded ? {elapsed_milliseconds: 18_420, peak_memory_bytes: 24 * 1024 ** 3} : null,
    status_reason: state === "failed" ? "The assigned Spark rejected the output manifest. Review the recipe output slots, then retry with local inputs." : null,
    timeout_seconds: 3600, created_at: createdAt, updated_at: createdAt,
  };
}

async function installArtifactFixture(page: Page) {
  const detail = artifactDetail();
  const state = {mode: "normal" as FixtureMode};
  fixtureStates.set(page, state);
  const normalJobs = [
    artifactJob("00000000-0000-4000-8000-000000000023", "running", "2026-08-28T12:04:00Z"),
    artifactJob("00000000-0000-4000-8000-000000000022", "ready", "2026-08-28T12:02:00Z"),
    artifactJob("00000000-0000-4000-8000-000000000021", "succeeded", "2026-08-28T11:55:00Z"),
    artifactJob("00000000-0000-4000-8000-000000000020", "failed", "2026-08-28T11:42:00Z"),
  ];
  await page.route("**/api/v1/**", route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/session") return route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}});
    if (path === "/api/v1/catalog/public-recipes") return route.fulfill({json: {repository: "fixture", commit: "a".repeat(40), recipes: []}});
    if (path === "/api/v1/library") return route.fulfill({json: librarySnapshot});
    if (path === "/api/v1/library/recipes/recipe-chat") return route.fulfill({json: detail});
    if (path === "/api/v1/artifact-jobs/capabilities") return route.fulfill({json: {
      schema_version: 1,
      transport: {max_input_files: 32, max_input_file_bytes: 512 * 1024 ** 2, max_input_total_bytes: 1024 ** 3, max_output_files: 32, max_output_file_bytes: 1024 ** 3, max_output_total_bytes: 2 * 1024 ** 3, max_timeout_seconds: 3600, reserved_input_names: ["manifest.json"]},
      storage: {max_stored_bytes: 4 * 1024 ** 3, used_bytes: 768 * 1024 ** 2, remaining_bytes: 3.25 * 1024 ** 3},
    }});
    if (path === `/api/v1/recipes/runs/${runId}/artifact-jobs`) {
      const jobs = state.mode === "empty" ? [] : state.mode === "failed"
        ? [artifactJob("00000000-0000-4000-8000-000000000024", "failed", "2026-08-28T12:08:00Z")]
        : normalJobs;
      return route.fulfill({json: {jobs}});
    }
    if (path.endsWith("/results/" + "d".repeat(64))) return route.fulfill({contentType: "image/svg+xml", body: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 540"><defs><linearGradient id="sky" x2="1" y2="1"><stop stop-color="#07110e"/><stop offset=".55" stop-color="#123d36"/><stop offset="1" stop-color="#87d84d"/></linearGradient><radialGradient id="glow"><stop stop-color="#d8ff9c" stop-opacity=".95"/><stop offset="1" stop-color="#3ba876" stop-opacity="0"/></radialGradient></defs><rect width="960" height="540" fill="url(#sky)"/><circle cx="690" cy="175" r="240" fill="url(#glow)"/><path d="M0 430 190 290l150 88 150-205 135 153 145-82 190 186v110H0z" fill="#091512" opacity=".88"/><path d="M0 468 230 343l119 69 139-183 146 149 126-71 200 159" fill="none" stroke="#9cf25d" stroke-width="5" opacity=".75"/><text x="52" y="78" fill="#edffe0" font-family="system-ui,sans-serif" font-size="34" font-weight="700">AURORA / TITANIUM STUDY</text><text x="54" y="116" fill="#b9d8c2" font-family="system-ui,sans-serif" font-size="18">controller fixture · immutable output preview</text></svg>`});
    if (path.includes("/results/")) return route.fulfill({status: 204});
    return route.fulfill({status: 404, json: {detail: `No fixture for ${request.method()} ${path}`}});
  });
}

async function expectNoAccessibilityViolations(page: Page) {
  const results = await new AxeBuilder({page}).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations, results.violations.map(item => `${item.id}: ${item.help}`).join("\n")).toEqual([]);
}

test.beforeEach(async ({page}) => installArtifactFixture(page));

test("renders the real artifact workcell with durable active and multi-output history", async ({page}) => {
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto("/library/recipes/recipe-chat");
  const workcell = page.getByRole("region", {name: "Create artifacts"});
  await expect(workcell).toBeVisible();
  await expect(workcell.getByText("Run ready for jobs")).toBeVisible();
  await expect(workcell.getByRole("article", {name: /running/i})).toBeVisible();
  await expect(workcell.getByRole("article", {name: /ready/i})).toBeVisible();
  const succeeded = workcell.getByRole("article", {name: /succeeded/i});
  await expect(succeeded.getByRole("img", {name: "Generated output aurora-frame.png"})).toBeVisible();
  await expect(succeeded.getByLabel(/Listen to aurora-score.wav/)).toBeVisible();
  await expect(succeeded.getByText("3D artifact ready")).toBeVisible();
  await expect(succeeded.getByRole("link", {name: "Download"})).toHaveCount(3);
  const workcellBox = await workcell.boundingBox();
  expect(workcellBox?.width).toBeGreaterThan(700);
  const formBox = await workcell.getByRole("button", {name: "Submit artifact job"}).locator("xpath=ancestor::form").boundingBox();
  const historyBox = await workcell.getByRole("region", {name: "Artifact job history"}).boundingBox();
  expect(historyBox!.x).toBeGreaterThan(formBox!.x + formBox!.width - 2);
  const disclosureMetrics = await page.locator(".technical-details > summary").evaluateAll(elements => elements.map(element => ({
    height: element.getBoundingClientRect().height,
    icons: element.querySelectorAll("svg").length,
  })).filter(item => item.height > 0));
  expect(disclosureMetrics.length).toBeGreaterThan(4);
  expect(disclosureMetrics.every(item => item.height >= 44 && item.icons >= 2)).toBe(true);
  const lifecycleGeometry = await page.locator(".lifecycle-stage").evaluateAll(elements => elements.map(element => {
    const marker = element.querySelector(".lifecycle-marker")!.getBoundingClientRect();
    const label = element.querySelector("strong")!.getBoundingClientRect();
    return {markerBottom: marker.bottom, labelTop: label.top};
  }));
  expect(lifecycleGeometry.every(item => item.labelTop >= item.markerBottom)).toBe(true);
  await page.keyboard.press("Tab");
  await expect(page.locator(":focus-visible")).toBeVisible();
  await expectNoAccessibilityViolations(page);
});

test("exposes explicit empty and failed retry recovery states", async ({page}) => {
  const state = fixtureStates.get(page)!;
  state.mode = "empty";
  await page.goto("/library/recipes/recipe-chat");
  await expect(page.getByText("No artifact jobs yet")).toBeVisible();
  await expect(page.getByText(/first submitted job will stay here/i)).toBeVisible();

  state.mode = "failed";
  await page.reload();
  const failed = page.getByRole("article", {name: /failed/i});
  await expect(failed).toContainText("rejected the output manifest");
  await failed.getByRole("button", {name: "Prepare retry"}).click();
  await expect(page.getByText(/Retry prepared from/)).toBeVisible();
  await expectNoAccessibilityViolations(page);
});

test("captures the artifact workcell review surfaces", async ({page}) => {
  test.skip(process.env.IMPECCABLE_REVIEW !== "1", "Set IMPECCABLE_REVIEW=1 to write local review screenshots.");
  await mkdir(reviewDirectory, {recursive: true});
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto("/library/recipes/recipe-chat");
  const workcell = page.getByRole("region", {name: "Create artifacts"});
  await expect(workcell).toBeVisible();
  await workcell.getByRole("textbox", {name: "Prompt"}).fill("An aurora-lit product scene with precise titanium geometry and a restrained ambient score.");
  await expect(workcell.getByText("Ready to submit")).toBeVisible();
  await page.screenshot({path: resolve(reviewDirectory, "artifact-job-desktop.png"), fullPage: true});
  await page.setViewportSize({width: 390, height: 844});
  await page.reload();
  const mobileWorkcell = page.getByRole("region", {name: "Create artifacts"});
  await expect(mobileWorkcell).toBeVisible();
  const skipLink = page.getByRole("link", {name: "Skip to content"});
  await expect(skipLink).toHaveCSS("clip-path", "inset(50%)");
  await expect.poll(() => skipLink.evaluate(element => ({height: element.clientHeight, width: element.clientWidth}))).toEqual({height: 1, width: 1});
  await page.evaluate(() => {
    document.body.tabIndex = -1;
    document.body.focus();
  });
  await page.keyboard.press("Tab");
  await expect(skipLink).toBeFocused();
  await expect(skipLink).toBeVisible();
  await expect(skipLink).toHaveCSS("clip-path", "none");
  const skipLinkBox = await skipLink.boundingBox();
  expect(skipLinkBox!.x).toBeGreaterThanOrEqual(8);
  expect(skipLinkBox!.y).toBeGreaterThanOrEqual(8);
  expect(skipLinkBox!.x + skipLinkBox!.width).toBeLessThanOrEqual(382);
  await page.evaluate(() => document.body.removeAttribute("tabindex"));
  await mobileWorkcell.getByRole("textbox", {name: "Prompt"}).fill("An aurora-lit product scene with precise titanium geometry and a restrained ambient score.");
  await expect(skipLink).toHaveCSS("clip-path", "inset(50%)");
  await expect.poll(() => skipLink.evaluate(element => ({height: element.clientHeight, width: element.clientWidth}))).toEqual({height: 1, width: 1});
  await expect(page.locator(".recipe-qualification-disclosure")).not.toHaveAttribute("open", "");
  await expect(page.locator(".artifact-job-archive")).not.toHaveAttribute("open", "");
  const failedBadge = page.locator(".artifact-job-archive > summary .status-pill", {hasText: "Failed"});
  await expect(failedBadge).toHaveCSS("white-space", "nowrap");
  await expect(failedBadge).toHaveCSS("flex-shrink", "0");
  const failedBadgeMetrics = await failedBadge.evaluate(element => ({
    clientWidth: element.clientWidth,
    height: element.getBoundingClientRect().height,
    scrollWidth: element.scrollWidth,
  }));
  expect(failedBadgeMetrics.scrollWidth).toBeLessThanOrEqual(failedBadgeMetrics.clientWidth);
  expect(failedBadgeMetrics.height).toBeLessThan(32);
  await expect(mobileWorkcell.getByRole("article", {name: /running/i})).toBeVisible();
  await expect(mobileWorkcell.getByRole("article", {name: /succeeded/i})).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThan(6_000);
  await page.screenshot({path: resolve(reviewDirectory, "artifact-job-mobile.png"), fullPage: true});

  const state = fixtureStates.get(page)!;
  state.mode = "empty";
  await page.setViewportSize({width: 1120, height: 900});
  await page.reload();
  const emptyWorkcell = page.getByRole("region", {name: "Create artifacts"});
  await expect(emptyWorkcell.getByText("No artifact jobs yet")).toBeVisible();
  await emptyWorkcell.screenshot({path: resolve(reviewDirectory, "artifact-job-empty.png")});

  state.mode = "failed";
  await page.reload();
  const failedWorkcell = page.getByRole("region", {name: "Create artifacts"});
  await failedWorkcell.getByRole("button", {name: "Prepare retry"}).click();
  await expect(failedWorkcell.getByText(/Retry prepared from/)).toBeVisible();
  await failedWorkcell.screenshot({path: resolve(reviewDirectory, "artifact-job-failed-retry.png")});
});
