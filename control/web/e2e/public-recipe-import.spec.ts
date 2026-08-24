import AxeBuilder from "@axe-core/playwright";
import {expect, test, type Page, type TestInfo} from "@playwright/test";

const digest = (value: string) => value.repeat(64).slice(0, 64);

function publicRecipe(slug: string, overrides: Record<string, unknown> = {}) {
  return {
    publisher: "vonk-forge", slug, title: `${slug} production recipe`, description: `A digest-bound ${slug} recipe.`, tags: [slug],
    uri: `vonk://catalog/vonk-forge/${slug}@sha256:${digest(slug)}`, content_sha256: digest(slug),
    model_publisher: "models", model_slug: slug, model_title: slug, source_owner: "MiaLabs",
    source_repository: `https://github.com/MiaLabs/${slug}`, capabilities: ["chat", "reasoning"],
    qualification: "candidate", qualification_basis: "explicit-candidate-metadata",
    qualification_detail: "This immutable recipe explicitly declares candidate qualification.", precision: "FP8",
    execution_harness: "vllm-openai", runtime_distribution: "vllm-0-27-1", source_bundle_sha256: "9".repeat(64),
    artifact_count: 2, topology_name: "spark-pair", topology_mode: "tensor-parallel", node_count: 2,
    expected_download_bytes: 180 * 1024 ** 3, maximum_installed_bytes_per_node: 220 * 1024 ** 3,
    maximum_runtime_memory_bytes_per_node: 96 * 1024 ** 3, release_version: "1.2.0", release_released_at: "2026-08-24",
    local: {status: "update-available", recipe_id: "local-recipe", revision_number: 1, content_sha256: "1".repeat(64), release_version: "1.0.0"},
    ...overrides,
  };
}

const recipes = [
  publicRecipe("DeepSeek V3.1 2×Spark"),
  publicRecipe("GLM 5.2 3×Spark", {node_count: 3, capabilities: ["chat", "vision"]}),
  publicRecipe("GLM 5.2 4×Spark", {node_count: 4, source_owner: "Z.ai", qualification: "cataloged", qualification_basis: "explicit-accepted-metadata", qualification_detail: "The reviewed immutable recipe explicitly declares accepted qualification."}),
];

async function installFixtures(page: Page) {
  await page.route("**/api/v1/auth/session", route => route.fulfill({json: {subject: "admin", role: "administrator", expires_at: "2099-01-01T00:00:00Z"}}));
  await page.route("**/api/v1/catalog/public-recipes", route => route.fulfill({json: {repository: "CarstVaartjes/vonk-forge-recipes", commit: "a".repeat(40), recipes}}));
  await page.route("**/api/v1/catalog/imports/public/preview", async route => {
    const body = await route.request().postDataJSON() as {uri: string};
    const selected = recipes.find(recipe => recipe.uri === body.uri) ?? recipes[0];
    await route.fulfill({json: {...selected, source: "recipe_library", changes_since_local: [{version: "1.2.0", released_at: "2026-08-24", content_sha256: selected.content_sha256, upgrade_effect: "rebuild", changes: [{kind: "performance", summary: "Improved distributed defaults", details: "Uses the current upstream topology guidance.", references: ["https://github.com/MiaLabs"]}]}]}});
  });
  await page.route("**/api/v1/catalog/imports/public", route => route.fulfill({json: {recipe_id: "local-recipe", revision_number: 2, lifecycle: "draft", slug: "deepseek"}}));
}

async function expectNoSeriousAccessibilityViolations(page: Page) {
  const results = await new AxeBuilder({page}).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations, results.violations.map(value => `${value.id}: ${value.help}`).join("\n")).toEqual([]);
}

async function attachScreenshot(page: Page, testInfo: TestInfo, name: string) {
  await testInfo.attach(name, {body: await page.screenshot({fullPage: true}), contentType: "image/png"});
}

test.beforeEach(async ({page}) => installFixtures(page));

test("desktop catalog is accessible, filterable and explains candidate qualification", async ({page}, testInfo) => {
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto("/library/import");
  await expect(page.getByRole("heading", {name: "Import a public recipe"})).toBeFocused();
  await expect(page.getByRole("option", {name: /4\+ Sparks \(1\)/})).toBeEnabled();
  await expect(page.getByRole("option", {name: /^4 Sparks/})).toHaveCount(0);
  await page.getByRole("checkbox", {name: /Chat/}).check();
  await page.getByRole("checkbox", {name: /Reasoning/}).check();
  await expect(page.getByRole("heading", {name: "DeepSeek V3.1 2×Spark"})).toBeVisible();
  await expect(page.getByRole("heading", {name: "GLM 5.2 3×Spark"})).toHaveCount(0);
  await page.getByRole("button", {name: /Review update for DeepSeek V3.1/}).click();
  await expect(page.getByRole("heading", {name: /DeepSeek V3.1 2×Spark production recipe/})).toBeFocused();
  await expect(page.getByText("This immutable recipe explicitly declares candidate qualification.")).toBeVisible();
  await expect(page.getByLabel("Version summary")).toContainText("v1.0.0");
  await expect(page.getByLabel("Version summary")).toContainText("v1.2.0");
  await expectNoSeriousAccessibilityViolations(page);
  await attachScreenshot(page, testInfo, "public-recipe-import-desktop.png");
});

test("mobile uses Catalog → Review → Confirm and preserves usable targets", async ({page}, testInfo) => {
  await page.setViewportSize({width: 360, height: 800});
  await page.goto("/library/import");
  await page.getByRole("button", {name: /Review update for DeepSeek V3.1/}).click();
  await expect(page.getByRole("complementary", {name: "Selected recipe review"})).toBeVisible();
  await expect(page.getByRole("main").filter({has: page.getByRole("heading", {name: "Choose a recipe"})})).toBeHidden();
  await page.getByRole("button", {name: "Continue to confirm"}).click();
  await expect(page.getByRole("button", {name: /Import v1.2.0/})).toBeVisible();
  for (const control of await page.locator("button:visible, a.button:visible, input:not([type=checkbox]):visible, select:visible").all()) {
    expect((await control.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(44);
  }
  await expectNoSeriousAccessibilityViolations(page);
  await attachScreenshot(page, testInfo, "public-recipe-import-mobile-confirm.png");
});

test("responsive breakpoints do not create document overflow", async ({page}) => {
  for (const width of [320, 360, 768, 895, 896, 1280, 1920]) {
    await page.setViewportSize({width, height: 900});
    await page.goto("/library/import");
    await expect.poll(() => page.evaluate(() => ({body: document.body.scrollWidth, root: document.documentElement.scrollWidth, viewport: document.documentElement.clientWidth}))).toEqual({body: width, root: width, viewport: width});
  }
});
