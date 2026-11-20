import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, expect, test } from "vitest";
import { setTheme } from "./lib/theme";

const script = readFileSync(resolve(process.cwd(), "public/theme-init.js"), "utf8");
const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");

afterEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

test("index.html loads theme-init.js before the module script", () => {
  expect(html.indexOf("/theme-init.js")).toBeGreaterThan(-1);
  expect(html.indexOf("/theme-init.js")).toBeLessThan(html.indexOf('type="module"'));
});

test("the initializer applies a pick stored by lib/theme and ignores anything else", () => {
  setTheme("light");
  delete document.documentElement.dataset.theme;
  new Function(script)();
  expect(document.documentElement.dataset.theme).toBe("light");
  delete document.documentElement.dataset.theme;
  localStorage.setItem("vonk-theme", "sepia");
  new Function(script)();
  expect(document.documentElement.dataset.theme).toBeUndefined();
});
