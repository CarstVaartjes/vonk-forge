import AxeBuilder from "@axe-core/playwright";
import {expect, test, type Page, type TestInfo} from "@playwright/test";

const digest = (value: string) => value.repeat(64).slice(0, 64);

function publicRecipe(slug: string, overrides: Record<string, unknown> = {}) {
  return {
    publisher: "vonk-forge", slug, title: `${slug} production recipe`, description: `A digest-bound ${slug} recipe.`, tags: [slug],
    uri: `vonk://catalog/vonk-forge/${slug}@sha256:${digest(slug)}`, content_sha256: digest(slug),
    model_publisher: "models", model_slug: slug, model_title: slug,
    model_version_publisher: "models", model_version_slug: `${slug}-fp8`, model_version_title: `${slug} FP8`, source_owner: "MiaLabs",
    source_repository: `https://github.com/MiaLabs/${slug}`, capabilities: ["chat", "reasoning"],
    qualification: "candidate", qualification_basis: "explicit-candidate-metadata",
    qualification_detail: "This immutable recipe explicitly declares candidate qualification.", precision: "FP8", quantizations: ["FP8"],
    execution_readiness: "executable", execution_readiness_basis: "explicit-executable-metadata",
    execution_readiness_detail: "This recipe explicitly declares a complete executable runtime contract; fleet compatibility and operator review still apply.",
    execution_harness: "vllm-openai", runtime_distribution: "vllm-0-27-1", source_bundle_sha256: "9".repeat(64),
    artifact_count: 2, topology_name: "spark-pair", topology_mode: "tensor-parallel", node_count: 2,
    topology_roles: [{name: "entrypoint", count: 1, endpoint_owner: true}, {name: "shard", count: 1, endpoint_owner: false}],
    fabric: {connectivity: "switch", minimum_bandwidth_mbps: 200_000},
    expected_download_bytes: 180 * 1024 ** 3, maximum_installed_bytes_per_node: 220 * 1024 ** 3,
    maximum_runtime_memory_bytes_per_node: 96 * 1024 ** 3, release_version: "1.2.0", release_released_at: "2026-08-24",
    local: {status: "update-available", recipe_id: "local-recipe", revision_number: 1, content_sha256: "1".repeat(64), release_version: "1.0.0"},
    ...overrides,
  };
}

const recipes = [
  publicRecipe("DeepSeek V3.1 2×Spark"),
  publicRecipe("GLM 5.2 3×Spark", {node_count: 3, capabilities: ["chat", "vision"], execution_readiness: "integration-required", execution_readiness_basis: "explicit-integration-required-metadata", execution_readiness_detail: "Runtime integration is required.", topology_roles: [{name: "entrypoint", count: 1, endpoint_owner: true}, {name: "shard", count: 2, endpoint_owner: false}]}),
  publicRecipe("GLM 5.2 4×Spark", {node_count: 4, source_owner: "Z.ai", qualification: "cataloged", qualification_basis: "explicit-accepted-metadata", qualification_detail: "The reviewed immutable recipe explicitly declares accepted qualification.", execution_readiness: "not-declared", execution_readiness_basis: "missing-readiness-metadata", execution_readiness_detail: "Execution readiness is not declared.", topology_roles: [{name: "entrypoint", count: 1, endpoint_owner: true}, {name: "shard", count: 3, endpoint_owner: false}]}),
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
  await page.getByRole("button", {name: "More filters"}).click();
  await expect(page.getByRole("option", {name: /4\+ Sparks \(1\)/})).toBeEnabled();
  await expect(page.getByRole("option", {name: /Executable contract \(1\)/})).toBeEnabled();
  await expect(page.getByRole("option", {name: /^4 Sparks/})).toHaveCount(0);
  await page.getByRole("checkbox", {name: /Chat/}).check();
  await page.getByRole("checkbox", {name: /Reasoning/}).check();
  await expect(page.getByRole("heading", {name: "DeepSeek V3.1 2×Spark"})).toBeVisible();
  await expect(page.getByRole("heading", {name: "GLM 5.2 3×Spark"})).toHaveCount(0);
  await page.getByRole("button", {name: /Review update for DeepSeek V3.1/}).click();
  await expect(page.getByRole("heading", {name: /DeepSeek V3.1 2×Spark production recipe/})).toBeFocused();
  await expect(page.getByText("This immutable recipe explicitly declares candidate qualification.")).toBeVisible();
  await expect(page.getByText(/complete executable runtime contract/)).toBeVisible();
  await expect(page.getByLabel("Version summary")).toContainText("v1.0.0");
  await expect(page.getByLabel("Version summary")).toContainText("v1.2.0");
  await expectNoSeriousAccessibilityViolations(page);
  await attachScreenshot(page, testInfo, "public-recipe-import-desktop.png");
});

test("compact preference, comparison and graphical requirements stay human-readable", async ({page}, testInfo) => {
  await page.setViewportSize({width: 1280, height: 900});
  await page.goto("/library/import");
  await expect(page.getByRole("button", {name: "Detailed"})).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("button", {name: /Show filters/})).toBeHidden();
  await page.getByRole("button", {name: "Compact"}).click();
  await expect(page.locator(".public-import-recipe-list")).toHaveClass(/is-compact/);
  await page.reload();
  await expect(page.getByRole("button", {name: "Compact"})).toHaveAttribute("aria-pressed", "true");

  await page.getByRole("checkbox", {name: /Compare.*DeepSeek/}).check();
  await page.getByRole("checkbox", {name: /Compare.*GLM 5.2 3/}).check();
  await page.getByRole("button", {name: "Compare 2 recipes"}).click();
  await expect(page.getByRole("table", {name: "Selected public recipe comparison"})).toContainText("AbliteratedFalseFalse");
  await expect(page.getByText("a".repeat(40))).toBeHidden();

  await page.getByRole("button", {name: /Review update for DeepSeek V3.1/}).click();
  await expect(page.getByRole("region", {name: "2 Sparks · Tensor parallel"})).toBeVisible();
  await expect(page.getByRole("meter")).toHaveCount(3);
  await expect(page.getByText(`sha256:${digest("DeepSeek V3.1 2×Spark")}`)).toBeHidden();
  await page.getByText("Technical details").click();
  await expect(page.getByText(`sha256:${digest("DeepSeek V3.1 2×Spark")}`)).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
  await attachScreenshot(page, testInfo, "public-recipe-import-compact-and-topology.png");
});

test("wide review keeps navigation reachable and hands current recipes to install controls", async ({page}) => {
  const current = publicRecipe("Current 2×Spark", {local: {status: "current", recipe_id: "local-recipe", revision_number: 2, content_sha256: digest("Current 2×Spark"), release_version: "1.2.0"}});
  await page.unroute("**/api/v1/catalog/public-recipes");
  await page.route("**/api/v1/catalog/public-recipes", route => route.fulfill({json: {repository: "CarstVaartjes/vonk-forge-recipes", commit: "a".repeat(40), recipes: [current]}}));
  await page.unroute("**/api/v1/catalog/imports/public/preview");
  await page.route("**/api/v1/catalog/imports/public/preview", route => route.fulfill({json: {...current, source: "recipe_library", changes_since_local: []}}));

  await page.setViewportSize({width: 1920, height: 900});
  await page.goto(`/library/import?recipe=${encodeURIComponent(current.uri)}`);
  const review = page.getByRole("complementary", {name: "Selected recipe review"});
  const localLink = page.getByRole("link", {name: "Open build & install controls"});
  await expect(review).toHaveCSS("overflow-y", "auto");
  await expect(localLink).toBeVisible();
  await expect(localLink).toHaveAttribute("href", "/library/recipes/local-recipe");
  const reviewBox = await review.boundingBox();
  const linkBox = await localLink.boundingBox();
  expect(reviewBox?.height ?? 0).toBeLessThanOrEqual(868);
  expect((linkBox?.y ?? 900) + (linkBox?.height ?? 1)).toBeLessThanOrEqual(900);

  await localLink.click();
  await expect(page).toHaveURL(/\/library\/recipes\/local-recipe$/);
});

test("mobile uses Catalog → Review → Confirm and preserves usable targets", async ({page}, testInfo) => {
  await page.setViewportSize({width: 360, height: 800});
  await page.goto("/library/import?q=DeepSeek&sparks=2");
  const filterToggle = page.getByRole("button", {name: "Show filters 2 applied"});
  await expect(filterToggle).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("complementary", {name: "Recipe filters"})).toBeHidden();
  await expect(page.getByRole("heading", {name: "DeepSeek V3.1 2×Spark"})).toBeVisible();
  await expect(page.getByRole("heading", {name: "GLM 5.2 3×Spark"})).toHaveCount(0);
  await filterToggle.click();
  await expect(page.getByRole("button", {name: "Hide filters 2 applied"})).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("complementary", {name: "Recipe filters"})).toBeVisible();
  await expect(page.getByRole("searchbox", {name: "Find a recipe"})).toHaveValue("DeepSeek");
  await expect(page.getByLabel("Filter by required Sparks")).toHaveValue("2");
  await page.getByRole("button", {name: "Hide filters 2 applied"}).click();
  await expect(page).toHaveURL(/q=DeepSeek&sparks=2/);
  await page.getByRole("button", {name: /Review update for DeepSeek V3.1/}).click();
  await expect(page.getByRole("complementary", {name: "Selected recipe review"})).toBeVisible();
  await expect(page.getByRole("region", {name: "Choose a recipe"})).toBeHidden();
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
    if (width <= 896) {
      await expect(page.getByRole("button", {name: "Show filters"})).toBeVisible();
      await expect(page.getByRole("complementary", {name: "Recipe filters"})).toBeHidden();
    } else {
      await expect(page.getByRole("button", {name: "Show filters"})).toBeHidden();
      await expect(page.getByRole("complementary", {name: "Recipe filters"})).toBeVisible();
    }
    await expect.poll(() => page.evaluate(() => ({body: document.body.scrollWidth, root: document.documentElement.scrollWidth, viewport: document.documentElement.clientWidth}))).toEqual({body: width, root: width, viewport: width});
  }
});
