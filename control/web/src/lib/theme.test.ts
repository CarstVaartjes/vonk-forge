import { effectiveTheme, setTheme } from "./theme";

afterEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

test("a stored pick is the effective theme after a reload", () => {
  setTheme("light");
  delete document.documentElement.dataset.theme;
  expect(effectiveTheme()).toBe("light");
});

test("an unknown stored value is ignored and the system decides", () => {
  localStorage.setItem("vonk-theme", "purple");
  expect(effectiveTheme()).toBe("dark");
});
