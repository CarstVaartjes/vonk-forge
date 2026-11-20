import AxeBuilder from "@axe-core/playwright";
import { expect, type Locator, type Page } from "@playwright/test";

export async function expectNoSeriousAccessibilityViolations(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(
    results.violations,
    results.violations
      .map(
        (value) =>
          `${value.id}: ${value.help} @ ${value.nodes.map((node) => node.target.join(" ")).join(" | ")}`,
      )
      .join("\n"),
  ).toEqual([]);
}

/** Tab from the page start until `locator` is focused, then require a visible focus ring. */
export async function expectReachableByKeyboard(page: Page, locator: Locator) {
  for (let presses = 0; presses < 60; presses++) {
    await page.keyboard.press("Tab");
    if (await locator.evaluate((element) => element === document.activeElement)) break;
  }
  await expect(locator).toBeFocused();
  const ring = await locator.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      focusVisible: element.matches(":focus-visible"),
      outline: style.outlineStyle !== "none" && parseFloat(style.outlineWidth) > 0,
      shadow: style.boxShadow !== "none",
    };
  });
  expect(ring.focusVisible, "keyboard focus matches :focus-visible").toBe(true);
  expect(ring.outline || ring.shadow, "keyboard focus draws a visible ring").toBe(true);
}
