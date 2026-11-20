import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, expect, test } from "vitest";

const script = readFileSync(resolve(process.cwd(), "public/theme-init.js"), "utf8");
const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");

afterEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

test("index.html loads the theme initializer as a blocking same-origin script before the app", () => {
  expect(html).toContain('<script src="/theme-init.js"></script>');
  expect(html.indexOf("/theme-init.js")).toBeLessThan(html.indexOf("/src/main.tsx"));
});

test("the initializer applies a stored light pick and ignores anything else", () => {
  localStorage.setItem("vonk-theme", "light");
  new Function(script)();
  expect(document.documentElement.dataset.theme).toBe("light");
  delete document.documentElement.dataset.theme;
  localStorage.setItem("vonk-theme", "sepia");
  new Function(script)();
  expect(document.documentElement.dataset.theme).toBeUndefined();
});
