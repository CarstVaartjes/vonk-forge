import { applyStoredTheme, effectiveTheme, setTheme } from "./theme";

afterEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

test("a stored pick is applied and survives a reload", () => {
  setTheme("light");
  delete document.documentElement.dataset.theme;
  applyStoredTheme();
  expect(document.documentElement.dataset.theme).toBe("light");
  expect(effectiveTheme()).toBe("light");
});

test("without a pick the page follows the system and sets no attribute", () => {
  localStorage.setItem("vonk-theme", "purple");
  applyStoredTheme();
  expect(document.documentElement.dataset.theme).toBeUndefined();
  expect(effectiveTheme()).toBe("dark");
});
